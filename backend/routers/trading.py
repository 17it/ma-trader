"""
MA Trader - Trading Router
Endpoints for starting/stopping the bot, querying trades, manual ops, and K-line data.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import ccxt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from database import get_db, Trade, SymbolConfig, SettingModel, OperationLog, record_operation_log
from models import TradeResponse, TradingStatus, OperationLogResponse
from executor import Executor
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trading"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_bot(request: Request):
    return getattr(request.app.state, "bot", None)


def _get_ws_manager(request: Request):
    return getattr(request.app.state, "ws_manager", None)


def _get_config(request: Request):
    return getattr(request.app.state, "config", None)


def _build_exchange(settings: dict, require_auth: bool = False) -> Optional[object]:
    """Build a ccxt Binance Futures instance from settings dict with proxy support."""
    import os
    key = settings.get("binance_key", "") if require_auth else ""
    secret = settings.get("binance_secret", "") if require_auth else ""
    if require_auth and (not key or not secret):
        return None
    try:
        cfg = {
            "apiKey": key,
            "secret": secret,
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
            "timeout": 5000,
        }
        proxy = (settings.get("http_proxy") or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy:
            cfg["proxies"] = {"http": proxy, "https": proxy}
        exchange = ccxt.binance(cfg)
        return exchange
    except Exception as exc:
        logger.warning(f"Failed to build exchange: {exc}")
        return None


# ---------------------------------------------------------------------------
# Bot control
# ---------------------------------------------------------------------------

@router.post("/trading/start")
async def start_trading(request: Request, db: Session = Depends(get_db)):
    """Start the TradingBot."""
    from trader import TradingBot
    from data_feed import DataFeed
    from database import SessionLocal

    app = request.app
    if app.state.bot is not None and app.state.bot.running:
        return {"status": "already_running"}

    cfg: dict = app.state.config.get_all(db)
    is_paper = cfg.get("trading_mode", "paper") == "paper"
    exchange = _build_exchange(cfg, require_auth=not is_paper)

    notifier = TelegramNotifier(
        token=cfg.get("telegram_token"),
        chat_id=cfg.get("telegram_chat_id"),
    )

    data_feed = DataFeed(
        api_key=cfg.get("binance_key") if not is_paper else None,
        secret=cfg.get("binance_secret") if not is_paper else None,
        use_futures=True,
        proxy=cfg.get("http_proxy"),
    )

    executor = Executor(exchange=exchange, config=cfg)
    ws_manager = _get_ws_manager(request)

    bot = TradingBot(
        db_session_factory=SessionLocal,
        data_feed=data_feed,
        executor=executor,
        notifier=notifier,
    )
    # Register WS broadcast
    bot.register_ws_callback(lambda msg: asyncio.create_task(ws_manager.broadcast(msg)))
    app.state.bot = bot

    await bot.start()

    await ws_manager.broadcast({"type": "bot_status", "running": True})
    await notifier.notify_bot_started()

    return {"status": "started", "mode": "paper" if is_paper else "live"}


@router.post("/trading/stop")
async def stop_trading(request: Request):
    """Stop the TradingBot."""
    bot = _get_bot(request)
    ws_manager = _get_ws_manager(request)

    if bot is None or not bot.running:
        return {"status": "not_running"}

    await bot.stop()
    await ws_manager.broadcast({"type": "bot_status", "running": False})

    cfg = _get_config(request).get_all_cached()
    notifier = TelegramNotifier(
        token=cfg.get("telegram_token"),
        chat_id=cfg.get("telegram_chat_id"),
    )
    await notifier.notify_bot_stopped()

    return {"status": "stopped"}


@router.get("/trading/status", response_model=TradingStatus)
async def get_trading_status(request: Request, db: Session = Depends(get_db)):
    """Return bot running status and active symbol info."""
    bot = _get_bot(request)
    running = bot is not None and bot.running

    cfg_mgr = _get_config(request)
    cfg = cfg_mgr.get_all(db) if cfg_mgr else {}
    mode = cfg.get("trading_mode", "paper")

    active_symbols = []
    if bot is not None:
        status_info = bot.get_status()
        active_symbols = list(status_info.get("symbols", {}).keys())

    open_trades_count = db.query(Trade).filter(Trade.status == "open").count()

    return TradingStatus(
        running=running,
        mode=mode,
        active_symbols=active_symbols,
        open_trades=open_trades_count,
        daily_pnl=0.0,
    )


# ---------------------------------------------------------------------------
# Trade records
# ---------------------------------------------------------------------------

@router.get("/trades", response_model=list[TradeResponse])
def list_trades(
    db: Session = Depends(get_db),
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    is_paper: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Query trade records with optional filters."""
    q = db.query(Trade)
    if symbol:
        q = q.filter(Trade.symbol == symbol.upper())
    if status:
        q = q.filter(Trade.status == status)
    if is_paper is not None:
        q = q.filter(Trade.is_paper == is_paper)
    trades = q.order_by(Trade.open_time.desc()).limit(limit).all()
    return [TradeResponse.model_validate(t) for t in trades]


@router.get("/trades/{trade_id}", response_model=TradeResponse)
def get_trade(trade_id: int, db: Session = Depends(get_db)):
    """Get a single trade by ID."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return TradeResponse.model_validate(trade)


# ---------------------------------------------------------------------------
# Manual operations
# ---------------------------------------------------------------------------

@router.post("/simulate")
async def simulate_trade(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
):
    """
    Manually open a simulated (paper) trade.

    Body: { "symbol": "BTCUSDT", "direction": "long"|"short", "entry_price": float }
    """
    symbol = body.get("symbol", "").upper()
    direction = body.get("direction", "long")
    entry_price = float(body.get("entry_price", 0))

    if not symbol or entry_price <= 0:
        raise HTTPException(status_code=400, detail="symbol and entry_price are required")

    # Look up symbol config for SL/TP ratio
    sym_cfg = db.query(SymbolConfig).filter(SymbolConfig.symbol == symbol).first()
    rr = sym_cfg.rr_ratio if sym_cfg else 1.5
    risk_pct = sym_cfg.risk_per_trade if sym_cfg else 0.01
    timeframe = sym_cfg.timeframe if sym_cfg else "1m"
    confirm_count = sym_cfg.confirm_candles if sym_cfg else 2

    # Simple SL = 0.5 % away from entry
    sl_pct = 0.005
    if direction == "long":
        sl = entry_price * (1 - sl_pct)
        tp = entry_price + (entry_price - sl) * rr
    else:
        sl = entry_price * (1 + sl_pct)
        tp = entry_price - (sl - entry_price) * rr

    price_risk = abs(entry_price - sl)
    balance = 10_000.0  # default paper balance
    qty = balance * risk_pct / price_risk if price_risk > 0 else 0

    now = datetime.now(timezone.utc)
    trade = Trade(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=sl,
        take_profit=tp,
        quantity=round(qty, 4),
        status="open",
        is_paper=True,
        open_time=now,
        timeframe=timeframe,
        confirm_count=confirm_count,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    dir_label = "做多" if direction == "long" else "做空"
    record_operation_log(
        log_type="open",
        title=f"{symbol} {dir_label}开仓 (手动模拟)",
        message=f"入场价: {entry_price} | 止损: {stop_loss} | 止盈: {take_profit} | 数量: {quantity}",
        level="info",
        symbol=symbol,
        mode="paper",
        db=db,
    )

    ws_manager = _get_ws_manager(request)
    await ws_manager.broadcast({"type": "trade_opened", "trade": TradeResponse.model_validate(trade).model_dump(mode="json")})
    await ws_manager.broadcast({"type": "operation_log"})

    return TradeResponse.model_validate(trade)


@router.post("/trades/{trade_id}/close")
async def manual_close_trade(
    trade_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    """Manually close a paper trade at the given exit_price."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "open":
        raise HTTPException(status_code=400, detail="Trade is not open")
    if not trade.is_paper:
        raise HTTPException(status_code=400, detail="Manual close only supported for paper trades")

    exit_price = float(body.get("exit_price", trade.entry_price))

    if trade.direction == "long":
        pnl = (exit_price - trade.entry_price) * trade.quantity
    else:
        pnl = (trade.entry_price - exit_price) * trade.quantity
    pnl_pct = pnl / (trade.entry_price * trade.quantity) * 100 if trade.quantity else 0

    trade.exit_price = exit_price
    trade.pnl = round(pnl, 4)
    trade.pnl_pct = round(pnl_pct, 4)
    trade.close_time = datetime.now(timezone.utc)
    trade.close_reason = "manual"
    trade.status = "closed"
    db.commit()
    db.refresh(trade)

    pnl_sign = "+" if trade.pnl >= 0 else ""
    record_operation_log(
        log_type="close",
        title=f"{trade.symbol} 平仓 (manual)",
        message=f"入场: {trade.entry_price} | 出场: {exit_price} | 盈亏: {pnl_sign}${trade.pnl:.2f} ({pnl_sign}{trade.pnl_pct:.2f}%)",
        level="success" if trade.pnl >= 0 else "warning",
        symbol=trade.symbol,
        mode="paper" if trade.is_paper else "live",
        db=db,
    )

    # Sync in-memory bot positions so it doesn't keep monitoring or block new entries
    bot = _get_bot(request)
    if bot is not None and hasattr(bot, "positions"):
        bot.positions.pop(trade.symbol, None)

    ws_manager = _get_ws_manager(request)
    await ws_manager.broadcast({"type": "trade_closed", "trade": TradeResponse.model_validate(trade).model_dump(mode="json")})
    await ws_manager.broadcast({"type": "operation_log"})

    # Send Telegram close notification
    try:
        cfg_mgr = _get_config(request)
        cfg = cfg_mgr.get_all_cached() if cfg_mgr else {}
        notifier = TelegramNotifier(
            token=cfg.get("telegram_token"),
            chat_id=cfg.get("telegram_chat_id"),
        )
        emoji = "✅" if trade.pnl >= 0 else "❌"
        pnl_sign = "+" if trade.pnl >= 0 else ""
        tg_msg = (
            f"{emoji} {trade.symbol} closed (manual) [PAPER]\n"
            f"Entry: {trade.entry_price}  Exit: {exit_price}\n"
            f"PnL: {pnl_sign}${trade.pnl:.2f} ({pnl_sign}{trade.pnl_pct:.2f}%)"
        )
        await notifier.send(tg_msg)
    except Exception as exc:
        logger.warning(f"Manual close Telegram notification failed: {exc}")

    return TradeResponse.model_validate(trade)


@router.delete("/trades/{trade_id}")
@router.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a single trade record by ID."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status == "open":
        bot = _get_bot(request)
        if bot is not None and hasattr(bot, "positions"):
            pos = bot.positions.get(trade.symbol)
            pos_id = pos["id"] if isinstance(pos, dict) else getattr(pos, "id", None)
            if pos_id == trade_id:
                bot.positions.pop(trade.symbol, None)

    db.delete(trade)
    db.commit()
    return {"status": "deleted", "id": trade_id}


@router.delete("/trades")
@router.post("/trades/clear")
def clear_trades(request: Request, db: Session = Depends(get_db)):
    """Delete all trade records."""
    deleted_count = db.query(Trade).delete()
    db.commit()

    bot = _get_bot(request)
    if bot is not None and hasattr(bot, "positions"):
        bot.positions.clear()

    return {"status": "cleared", "deleted": deleted_count}


# ---------------------------------------------------------------------------
# K-line data
# ---------------------------------------------------------------------------

@router.get("/klines/{symbol}")
async def get_klines(
    symbol: str,
    request: Request,
    timeframe: str = Query("1m"),
    limit: int = Query(200, ge=10, le=1000),
    endTime: Optional[int] = Query(None, description="毫秒时间戳，获取早于此时间的K线"),
):
    """
    Return OHLCV + MA7/MA25/MA60 data for charting.

    Falls back to empty list when exchange is not configured.
    """
    import numpy as np

    config_mgr = getattr(request.app.state, "config", None)
    if config_mgr is not None and hasattr(config_mgr, "get_all_cached"):
        cfg = config_mgr.get_all_cached()
    else:
        from database import SessionLocal
        from config import get_all_settings
        db_s = SessionLocal()
        try:
            cfg = get_all_settings(db_s)
        finally:
            db_s.close()

    exchange = _build_exchange(cfg, require_auth=False)

    symbol = symbol.upper()
    try:
        limit_val = int(getattr(limit, "default", limit) or 200)
    except Exception:
        limit_val = 200

    try:
        tf_val = str(getattr(timeframe, "default", timeframe) or "1m")
    except Exception:
        tf_val = "1m"

    raw = []
    if exchange is not None and not endTime:
        try:
            raw = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, tf_val, limit=limit_val + 60)
        except Exception as exc:
            logger.warning(f"fetch_ohlcv for {symbol} failed: {exc}")

    # Fallback 1: Query public Binance Vision API (no geo restriction / 451 block)
    if not raw:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        fetch_limit = limit_val if endTime else (limit_val + 60)
        end_time_param = f"&endTime={endTime}" if endTime else ""
        endpoints = [
            f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}&interval={tf_val}&limit={fetch_limit}{end_time_param}",
            f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={tf_val}&limit={fetch_limit}{end_time_param}",
            f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={tf_val}&limit={fetch_limit}{end_time_param}",
        ]
        proxy = (cfg.get("http_proxy") or "").strip()
        proxies = None
        if proxy:
            p_str = proxy
            if p_str.startswith("socks5://"):
                p_str = "socks5h://" + p_str[len("socks5://"):]
            proxies = {"http": p_str, "https": p_str}

        import requests
        for endpoint in endpoints:
            try:
                resp = await asyncio.to_thread(requests.get, endpoint, proxies=proxies, timeout=6.0)
                if resp.status_code == 200:
                    items = resp.json()
                    if items and isinstance(items, list):
                        raw = [
                            [row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
                            for row in items
                        ]
                        logger.info(f"Loaded {len(raw)} real live Binance candles from {endpoint}")
                        break
            except Exception as e:
                logger.warning(f"Failed to fetch live candles from {endpoint}: {e}")

    # No synthetic/demonstration fallback allowed. If network fails, return 503 error.
    if not raw:
        logger.error(f"Failed to fetch real live candles for {symbol} from all Binance endpoints.")
        raise HTTPException(
            status_code=503,
            detail=f"无法获取 {symbol} 的实时真实K线数据，请检查网络连接或全局设置中的代理配置。"
        )

    closes = [b[4] for b in raw]
    n = len(closes)

    def _ma(period: int) -> list:
        result = [None] * n
        for i in range(period - 1, n):
            result[i] = round(sum(closes[i - period + 1: i + 1]) / period, 4)
        return result

    ma7 = _ma(7)
    ma25 = _ma(25)
    ma60 = _ma(60)

    data = []
    for i, bar in enumerate(raw):
        data.append({
            "time": bar[0],
            "open": bar[1],
            "high": bar[2],
            "low": bar[3],
            "close": bar[4],
            "volume": bar[5],
            "ma7": ma7[i],
            "ma25": ma25[i],
            "ma60": ma60[i],
        })

    # Return full data if paging history (endTime), otherwise last `limit` bars
    return {"symbol": symbol, "timeframe": tf_val, "data": data if endTime else data[-limit_val:]}


# ---------------------------------------------------------------------------
# Operation Logs
# ---------------------------------------------------------------------------

@router.get("/logs", response_model=list[OperationLogResponse])
def list_operation_logs(
    log_type: Optional[str] = Query(None, description="Filter by log_type: open, close, start, stop"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Return persistent operation logs ordered newest first."""
    q = db.query(OperationLog)
    if log_type:
        q = q.filter(OperationLog.log_type == log_type)
    return q.order_by(OperationLog.id.desc()).offset(offset).limit(limit).all()


@router.delete("/logs")
@router.post("/logs/clear")
def clear_operation_logs(db: Session = Depends(get_db)):
    """Clear all operation logs."""
    deleted = db.query(OperationLog).delete()
    db.commit()
    return {"status": "cleared", "deleted": deleted}
