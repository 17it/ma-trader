"""
MA Trader - Backtest Router
Async backtest task management with bounded concurrency and retained task references.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Set

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db, BacktestRun
from models import BacktestRunCreate, BacktestRunResponse
from backtester import Backtester

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backtest"])

# Retain strong references to background tasks (prevents GC cancellation) & cap concurrency
_RUNNING_TASKS: Set[asyncio.Task] = set()
_BACKTEST_SEMAPHORE = asyncio.Semaphore(2)


def _get_exchange(request: Request):
    """Build exchange from cached config or anonymous ccxt client with proxy support."""
    import ccxt
    cfg = {}
    try:
        if hasattr(request.app.state, "config") and request.app.state.config:
            cfg = request.app.state.config.get_all_cached()
    except Exception:
        pass
    try:
        options = {
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
            "timeout": 10000,
        }
        proxy = (cfg.get("http_proxy") or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy:
            options["proxies"] = {"http": proxy, "https": proxy}
        return ccxt.binance(options)
    except Exception:
        return None


def _run_backtest_sync(run_id: int, params: dict, exchange):
    """Synchronous worker running in a background thread to prevent blocking the event loop."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if run is None:
            return
        run.status = "running"
        db.commit()

        backtester = Backtester(exchange=exchange)
        result = backtester.run(
            symbol=params["symbol"],
            timeframe=params["timeframe"],
            htf_timeframe=params.get("htf_timeframe", "15m"),
            start_date=params["start_date"],
            end_date=params["end_date"],
            confirm_candles=params.get("confirm_candles", 2),
            rr_ratio=params.get("rr_ratio", 1.5),
            initial_capital=params.get("initial_capital", 10_000.0),
            risk_per_trade=params.get("risk_per_trade", 0.01),
            leverage=params.get("leverage", 5),
            fee_rate=params.get("fee_rate", 0.0004),
            slippage_rate=params.get("slippage_rate", 0.0001),
        )

        run.results = json.dumps(result, ensure_ascii=False, default=str)
        run.status = "done"
        db.commit()
        logger.info("Backtest %d completed: %d trades", run_id, result["total_trades"])
    except Exception as exc:
        logger.error("Backtest %d failed: %s", run_id, exc)
        try:
            db.query(BacktestRun).filter(BacktestRun.id == run_id).update(
                {"status": "failed", "error_msg": str(exc)[:500]}
            )
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def _run_backtest_task(run_id: int, params: dict, exchange):
    """Non-blocking background coroutine with bounded concurrency."""
    async with _BACKTEST_SEMAPHORE:
        await asyncio.to_thread(_run_backtest_sync, run_id, params, exchange)


@router.post("/backtest", response_model=BacktestRunResponse, status_code=202)
async def create_backtest(
    body: BacktestRunCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a new backtest task. Runs asynchronously; returns immediately with the run id."""
    params = body.model_dump()
    if "params" in params and isinstance(params["params"], dict):
        for k, v in params["params"].items():
            if k not in params or params[k] is None:
                params[k] = v

    run = BacktestRun(
        title=body.title.strip() if body.title and body.title.strip() else None,
        symbol=body.symbol.upper(),
        timeframe=body.timeframe,
        start_date=body.start_date,
        end_date=body.end_date,
        params=json.dumps(params, ensure_ascii=False),
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    exchange = _get_exchange(request)
    task = asyncio.create_task(_run_backtest_task(run.id, params, exchange), name=f"backtest-{run.id}")
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)

    return BacktestRunResponse.model_validate(run)


@router.get("/backtest", response_model=list[BacktestRunResponse])
def list_backtests(
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """List backtest history (lightweight summary without heavy trades/equity_curve arrays)."""
    runs = db.query(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit).all()
    resp = []
    for r in runs:
        item = BacktestRunResponse.model_validate(r)
        if item.results:
            try:
                parsed = json.loads(item.results)
                if isinstance(parsed, dict):
                    parsed.pop("trades", None)
                    parsed.pop("equity_curve", None)
                    item.results = json.dumps(parsed, ensure_ascii=False)
            except Exception:
                pass
        resp.append(item)
    return resp


@router.get("/backtest/{run_id}", response_model=BacktestRunResponse)
def get_backtest(run_id: int, db: Session = Depends(get_db)):
    """Get a single backtest result including trades and equity_curve."""
    run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return BacktestRunResponse.model_validate(run)


@router.delete("/backtest", status_code=204)
def clear_all_backtests(db: Session = Depends(get_db)):
    """Clear all backtest history records."""
    db.query(BacktestRun).delete()
    db.commit()


@router.delete("/backtest/{run_id}", status_code=204)
def delete_backtest(run_id: int, db: Session = Depends(get_db)):
    """Delete a backtest run record."""
    run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    db.delete(run)
    db.commit()
