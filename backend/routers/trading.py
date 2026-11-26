"""
MA Trader - Trading Router
Endpoints for starting/stopping the bot, querying trades, manual ops, and K-line data.
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import ccxt
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import (
    get_db,
    Trade,
    SymbolConfig,
    OperationLog,
    SystemLog,
    record_operation_log,
    record_system_log,
)
from models import (
    ManualCloseRequest,
    OperationLogPageResponse,
    SimulateTradeRequest,
    SystemLogPageResponse,
    TradeResponse,
    TradingStatus,
    VALID_TIMEFRAMES,
    validate_symbol_str,
)
from executor import Executor
from notifier import TelegramNotifier
from strategy import MAStrategy
from trader import get_trade_close_lock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trading"])

# ---------------------------------------------------------------------------
# Cached public exchange singleton (avoids reloading markets on every /klines call)
# ---------------------------------------------------------------------------
_public_exchange_lock = threading.Lock()
_public_exchange_call_lock = threading.Lock()
_public_exchange_cache: dict = {"exchange": None, "proxy": None}


def invalidate_public_exchange_cache() -> None:
    with _public_exchange_lock:
        _public_exchange_cache["exchange"] = None
        _public_exchange_cache["proxy"] = None


def _get_cached_public_exchange(proxy: Optional[str]) -> Optional[object]:
    norm_proxy = (proxy or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
    with _public_exchange_lock:
        if _public_exchange_cache["exchange"] is not None and _public_exchange_cache["proxy"] == norm_proxy:
            return _public_exchange_cache["exchange"]
        try:
            cfg = {
                "options": {"defaultType": "future"},
                "enableRateLimit": True,
                "timeout": 8000,
            }
            if norm_proxy:
                cfg["proxies"] = {"http": norm_proxy, "https": norm_proxy}
            ex = ccxt.binance(cfg)
            _public_exchange_cache["exchange"] = ex
            _public_exchange_cache["proxy"] = norm_proxy
            return ex
        except Exception as exc:
            logger.warning("Failed to build cached public exchange: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_bots(request: Request) -> dict:
    bots = getattr(request.app.state, "bots", None)
    if bots is None:
        bots = {"paper": None, "live": None}
        request.app.state.bots = bots
    return bots


def _get_bot_lock(request: Request) -> asyncio.Lock:
    lock = getattr(request.app.state, "bot_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.bot_lock = lock
    return lock


def _bot_status_payload(bots: dict) -> dict:
    paper_bot = bots.get("paper")
    live_bot = bots.get("live")
    paper_running = bool(paper_bot is not None and paper_bot.running)
    live_running = bool(live_bot is not None and live_bot.running)
    return {
        "type": "bot_status",
        "running": paper_running or live_running,
        "paper_running": paper_running,
        "live_running": live_running,
    }


def _get_ws_manager(request: Request):
    return getattr(request.app.state, "ws_manager", None)


def _get_config(request: Request):
    return getattr(request.app.state, "config", None)


def _build_exchange(settings: dict, require_auth: bool = False) -> Optional[object]:
    """Build a ccxt Binance Futures instance from settings dict with proxy support."""
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
            "timeout": 10000,
        }
        proxy = (settings.get("http_proxy") or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy:
            cfg["proxies"] = {"http": proxy, "https": proxy}
        return ccxt.binance(cfg)
    except Exception as exc:
        logger.warning("Failed to build exchange: %s", exc)
        return None


def _normalize_httpx_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith("socks5h://"):
        return "socks5://" + p[len("socks5h://"):]
    return p or None


# ---------------------------------------------------------------------------
# Bot control
# ---------------------------------------------------------------------------

@router.post("/trading/start")
async def start_trading(
    request: Request,
    mode: str = Query("paper", pattern="^(paper|live)$"),
    db: Session = Depends(get_db),
):
    """Start the TradingBot for the requested mode ('paper' or 'live'), sharing the WS market stream."""
    from trader import TradingBot
    from data_feed import DataFeed
    from database import SessionLocal

    lock = _get_bot_lock(request)
    async with lock:
        app = request.app
        bots = _get_bots(request)
        existing = bots.get(mode)
        if existing is not None and existing.running:
            return {"status": "already_running", "mode": mode}

        cfg: dict = app.state.config.get_all(db)
        is_paper = (mode == "paper")
        if not is_paper and (not (cfg.get("binance_key") or "").strip() or not (cfg.get("binance_secret") or "").strip()):
            raise HTTPException(status_code=400, detail="实盘启动失败：请先在设置中配置 Binance API Key 和 Secret")

        exchange = _build_exchange(cfg, require_auth=not is_paper)
        if not is_paper and exchange is None:
            raise HTTPException(status_code=400, detail="实盘启动失败：无法初始化 Binance 交易所实例")

        notifier = TelegramNotifier(
            token=cfg.get("telegram_token"),
            chat_id=cfg.get("telegram_chat_id"),
            proxy=cfg.get("http_proxy") or None,
        )

        shared_ws = getattr(app.state, "ws_streamer", None)
        data_feed = DataFeed(
            api_key=cfg.get("binance_key") if not is_paper else None,
            secret=cfg.get("binance_secret") if not is_paper else None,
            use_futures=True,
            proxy=cfg.get("http_proxy"),
            ws_streamer=shared_ws,
            allow_spot_fallback=is_paper,
        )

        executor = Executor(exchange=exchange, config=cfg, is_paper=is_paper)
        ws_manager = _get_ws_manager(request)

        bot = TradingBot(
            db_session_factory=SessionLocal,
            data_feed=data_feed,
            executor=executor,
            notifier=notifier,
            ws_streamer=shared_ws,
            is_paper=is_paper,
        )
        bot.register_ws_callback(lambda msg: asyncio.create_task(ws_manager.broadcast(msg)))
        bots[mode] = bot

        await bot.start()

        await ws_manager.broadcast(_bot_status_payload(bots))
        await notifier.notify_bot_started()

        return {"status": "started", "mode": mode}


@router.post("/trading/stop")
async def stop_trading(
    request: Request,
    mode: Optional[str] = Query(None, pattern="^(paper|live)$"),
    db: Session = Depends(get_db),
):
    """Stop the TradingBot for the given mode ('paper' or 'live'), or all running bots if omitted."""
    lock = _get_bot_lock(request)
    async with lock:
        bots = _get_bots(request)
        ws_manager = _get_ws_manager(request)
        targets = [mode] if mode else ["paper", "live"]

        stopped_any = False
        live_stopped = False
        for m in targets:
            bot = bots.get(m)
            if bot is not None and bot.running:
                await bot.stop()
                stopped_any = True
                if m == "live":
                    live_stopped = True

        if not stopped_any:
            return {"status": "not_running", "mode": mode}

        status_payload = _bot_status_payload(bots)
        if not status_payload["running"]:
            shared_ws = getattr(request.app.state, "ws_streamer", None)
            if shared_ws is not None:
                await shared_ws.stop()

        await ws_manager.broadcast(status_payload)

        open_live_count = (
            db.query(Trade).filter(Trade.status == "open", Trade.is_paper == False).count()  # noqa: E712
            if live_stopped
            else 0
        )
        warning_msg = (
            f"注意：交易所仍有 {open_live_count} 笔实盘持仓未平仓，停止后仅剩交易所已挂止损单保护。"
            if open_live_count > 0
            else None
        )

        cfg = _get_config(request).get_all_cached()
        notifier = TelegramNotifier(
            token=cfg.get("telegram_token"),
            chat_id=cfg.get("telegram_chat_id"),
            proxy=cfg.get("http_proxy") or None,
        )
        await notifier.notify_bot_stopped()
        if warning_msg:
            await notifier.send(f"⚠️ {warning_msg}")

        resp = {"status": "stopped", "mode": mode}
        if warning_msg:
            resp["warning"] = warning_msg
            resp["open_live_trades"] = open_live_count
        return resp


@router.get("/trading/status", response_model=TradingStatus)
async def get_trading_status(request: Request, db: Session = Depends(get_db)):
    """Return bot running status (paper & live), active symbols, and today's realized PnL."""
    bots = _get_bots(request)
    paper_bot = bots.get("paper")
    live_bot = bots.get("live")
    paper_running = bool(paper_bot is not None and paper_bot.running)
    live_running = bool(live_bot is not None and live_bot.running)
    running = paper_running or live_running

    cfg_mgr = _get_config(request)
    cfg = cfg_mgr.get_all(db) if cfg_mgr else {}
    if paper_running and live_running:
        mode = "both"
    elif live_running:
        mode = "live"
    elif paper_running:
        mode = "paper"
    else:
        mode = cfg.get("trading_mode", "paper")

    active_symbols_set = set()
    started_at_candidates = []
    for bot in (paper_bot, live_bot):
        if bot is not None and bot.running:
            status_info = bot.get_status()
            active_symbols_set.update(status_info.get("symbols", {}).keys())
            started = getattr(bot, "_started_at", None)
            if started:
                started_at_candidates.append(started)

    open_trades_count = db.query(Trade).filter(Trade.status == "open").count()

    # Calculate today's UTC realized PnL across closed trades
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    today_q = db.query(Trade).filter(Trade.status == "closed", Trade.close_time >= today_start)
    if mode in ("paper", "live"):
        today_q = today_q.filter(Trade.is_paper == (mode == "paper"))
    today_trades = today_q.all()
    daily_pnl = sum(t.pnl or 0.0 for t in today_trades)
    base_equity = 10_000.0
    daily_pnl_pct = round(daily_pnl / base_equity * 100.0, 2) if base_equity > 0 else 0.0

    uptime_seconds = int(time.time() - min(started_at_candidates)) if started_at_candidates else 0

    return TradingStatus(
        running=running,
        paper_running=paper_running,
        live_running=live_running,
        mode=mode,
        active_symbols=sorted(active_symbols_set),
        open_trades=open_trades_count,
        daily_pnl=round(daily_pnl, 4),
        daily_pnl_pct=daily_pnl_pct,
        uptime_seconds=max(0, uptime_seconds),
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
    body: SimulateTradeRequest,
    db: Session = Depends(get_db),
):
    """
    Manually open a simulated (paper) trade and sync it with the running paper bot.
    """
    symbol = body.symbol
    direction = body.direction
    entry_price = body.entry_price

    # Prevent duplicate open paper position on the same symbol
    existing_open = (
        db.query(Trade)
        .filter(Trade.symbol == symbol, Trade.status == "open", Trade.is_paper == True)  # noqa: E712
        .first()
    )
    if existing_open:
        raise HTTPException(
            status_code=409,
            detail=f"{symbol} 已有一笔持仓中的模拟订单 (#{existing_open.id})，请先平仓后再开新仓",
        )

    sym_cfg = db.query(SymbolConfig).filter(SymbolConfig.symbol == symbol).first()
    rr = sym_cfg.rr_ratio if sym_cfg else 1.5
    risk_pct = sym_cfg.risk_per_trade if sym_cfg else 0.01
    leverage = sym_cfg.leverage if sym_cfg else 5
    timeframe = sym_cfg.timeframe if sym_cfg else "1m"
    confirm_count = sym_cfg.confirm_candles if sym_cfg else 2
    trailing_activate_r = (
        float(sym_cfg.trailing_activate_r)
        if sym_cfg and getattr(sym_cfg, "trailing_activate_r", None) is not None
        else 0.88
    )
    breakeven_buffer = (
        float(sym_cfg.breakeven_buffer)
        if sym_cfg and getattr(sym_cfg, "breakeven_buffer", None) is not None
        else 0.003
    )
    trailing_ma = (
        str(sym_cfg.trailing_ma).lower()
        if sym_cfg and getattr(sym_cfg, "trailing_ma", None)
        else "ma25"
    )

    sl_pct = 0.005
    if direction == "long":
        sl = entry_price * (1 - sl_pct)
        tp = entry_price + (entry_price - sl) * rr
    else:
        sl = entry_price * (1 + sl_pct)
        tp = entry_price - (sl - entry_price) * rr

    price_risk = abs(entry_price - sl)
    balance = 10_000.0
    qty = MAStrategy.calc_position_size(
        capital=balance,
        risk_pct=risk_pct,
        entry_price=entry_price,
        sl_distance=price_risk,
        leverage=leverage,
    )
    if qty <= 0:
        raise HTTPException(status_code=400, detail="计算得到的开仓数量无效")

    now = datetime.now(timezone.utc)
    trade = Trade(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=sl,
        take_profit=tp,
        quantity=qty,
        status="open",
        is_paper=True,
        open_time=now,
        timeframe=timeframe,
        confirm_count=confirm_count,
        initial_risk=price_risk,
        trailing_activate_r=trailing_activate_r,
        breakeven_buffer=breakeven_buffer,
        trailing_ma=trailing_ma,
        fee=0.0,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    # Register with running paper bot so SL/TP/trailing monitoring covers manual paper trades too
    bots = _get_bots(request)
    paper_bot = bots.get("paper")
    if paper_bot is not None and hasattr(paper_bot, "positions"):
        paper_bot.positions[symbol] = {
            "id": trade.id,
            "symbol": trade.symbol,
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "quantity": trade.quantity,
            "status": "open",
            "is_paper": True,
            "open_time": trade.open_time,
            "timeframe": trade.timeframe,
            "exchange_order_id": None,
            "sl_order_id": None,
            "trailing_active": False,
            "risk_r": price_risk,
            "trailing_activate_r": trailing_activate_r,
            "breakeven_buffer": breakeven_buffer,
            "trailing_ma": trailing_ma,
        }

    dir_label = "做多" if direction == "long" else "做空"
    record_operation_log(
        log_type="open",
        title=f"{symbol} {dir_label}开仓 (手动模拟)",
        message=f"入场价: {entry_price} | 止损: {sl:.4f} | 止盈: {tp:.4f} | 数量: {qty}",
        level="info",
        symbol=symbol,
        mode="paper",
        db=db,
    )
    record_system_log(
        f"[模拟开仓] {direction.upper()} {symbol} 价格:{entry_price} 止损:{sl:.4f} 止盈:{tp:.4f}",
        level="success",
        symbol=symbol,
        db=db,
    )

    ws_manager = _get_ws_manager(request)
    if ws_manager is not None:
        await ws_manager.broadcast({
            "type": "trade_opened",
            "trade": TradeResponse.model_validate(trade).model_dump(mode="json"),
        })
        await ws_manager.broadcast({"type": "operation_log"})

    return TradeResponse.model_validate(trade)


async def _resolve_live_price(request: Request, symbol: str, cfg: dict, allow_spot: bool = True) -> Optional[float]:
    """Fetch current market price from shared WS cache first, then DataFeed REST."""
    shared_ws = getattr(request.app.state, "ws_streamer", None)
    if shared_ws is not None:
        cached = shared_ws.get_cached_price(symbol)
        if cached and cached > 0:
            return float(cached)
    from data_feed import DataFeed
    feed = DataFeed(use_futures=True, proxy=cfg.get("http_proxy"), ws_streamer=shared_ws, allow_spot_fallback=allow_spot)
    return await asyncio.to_thread(feed.get_ticker, symbol)


@router.post("/trades/{trade_id}/close")
async def manual_close_trade(
    trade_id: int,
    request: Request,
    body: Optional[ManualCloseRequest] = Body(default=None),
    db: Session = Depends(get_db),
):
    """
    Manually close an open trade (supports both paper and live trades).
    Body is optional: if exit_price is omitted, fetches current market price (or real fill price for live).
    """
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "open":
        raise HTTPException(status_code=400, detail="Trade is not open")

    cfg_mgr = _get_config(request)
    cfg = cfg_mgr.get_all_cached() if cfg_mgr else {}
    bots = _get_bots(request)
    target_bot = bots.get("paper" if trade.is_paper else "live")

    requested_price = body.exit_price if body is not None else None
    exit_price: Optional[float] = requested_price

    close_lock = get_trade_close_lock(trade_id)
    async with close_lock:
        db.refresh(trade)
        if trade.status != "open":
            return TradeResponse.model_validate(trade)

        close_fee = float(getattr(trade, "fee", 0.0) or 0.0)
        final_reason = "manual"

        if not trade.is_paper:
            # Live close: execute real reduceOnly market close on Binance Futures
            executor = getattr(target_bot, "executor", None) if target_bot is not None else None
            if executor is None or executor.exchange is None:
                exchange = _build_exchange(cfg, require_auth=True)
                if exchange is None:
                    raise HTTPException(status_code=400, detail="实盘平仓失败：未配置有效的 Binance API 凭证")
                executor = Executor(exchange=exchange, config=cfg, is_paper=False)

            if exit_price is None:
                exit_price = await _resolve_live_price(request, trade.symbol, cfg, allow_spot=False) or trade.entry_price

            try:
                close_res = await asyncio.to_thread(executor.close_trade, trade, exit_price, "manual")
                if isinstance(close_res, dict):
                    if close_res.get("exit_price"):
                        exit_price = float(close_res["exit_price"])
                    if close_res.get("fee") is not None:
                        close_fee = float(close_res["fee"])
                    if close_res.get("already_closed_on_exchange") and close_res.get("close_reason"):
                        final_reason = str(close_res["close_reason"])
            except Exception as exc:
                rearmed_id = getattr(exc, "rearmed_sl_order_id", None)
                if rearmed_id:
                    trade.sl_order_id = rearmed_id
                    db.commit()
                    if target_bot is not None and trade.symbol in getattr(target_bot, "positions", {}):
                        target_bot.positions[trade.symbol]["sl_order_id"] = rearmed_id
                logger.error("Manual live close failed for trade #%s (%s): %s", trade_id, trade.symbol, exc)
                raise HTTPException(status_code=502, detail=f"交易所平仓失败: {exc}")
        else:
            if exit_price is None:
                exit_price = await _resolve_live_price(request, trade.symbol, cfg, allow_spot=True)
                if not exit_price or exit_price <= 0:
                    raise HTTPException(
                        status_code=503,
                        detail=f"无法获取 {trade.symbol} 当前实时市价，请稍后重试或手动传入 exit_price",
                    )

        # Re-check open status inside lock before writing final P&L
        db.refresh(trade)
        if trade.status != "open":
            return TradeResponse.model_validate(trade)

        if trade.direction == "long":
            gross_pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.quantity
        pnl = gross_pnl - close_fee
        notional = trade.entry_price * trade.quantity
        pnl_pct = (pnl / notional * 100.0) if notional > 0 else 0.0

        trade.exit_price = exit_price
        trade.pnl = round(pnl, 4)
        trade.pnl_pct = round(pnl_pct, 4)
        trade.fee = round(close_fee, 6)
        trade.close_time = datetime.now(timezone.utc)
        trade.close_reason = final_reason
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
    record_system_log(
        f"[手动平仓] {trade.symbol} #{trade.id} 出场:{exit_price} 盈亏:{pnl_sign}${trade.pnl:.2f} ({pnl_sign}{trade.pnl_pct:.2f}%)",
        level="success" if trade.pnl >= 0 else "warning",
        symbol=trade.symbol,
        db=db,
    )

    if target_bot is not None and hasattr(target_bot, "positions"):
        pos = target_bot.positions.get(trade.symbol)
        pos_id = pos["id"] if isinstance(pos, dict) else getattr(pos, "id", None)
        if pos_id == trade.id or pos_id is None:
            target_bot.positions.pop(trade.symbol, None)

    ws_manager = _get_ws_manager(request)
    if ws_manager is not None:
        await ws_manager.broadcast({
            "type": "trade_closed",
            "trade": TradeResponse.model_validate(trade).model_dump(mode="json"),
        })
        await ws_manager.broadcast({"type": "operation_log"})

    try:
        notifier = TelegramNotifier(
            token=cfg.get("telegram_token"),
            chat_id=cfg.get("telegram_chat_id"),
            proxy=cfg.get("http_proxy") or None,
        )
        mode_tag = "PAPER" if trade.is_paper else "LIVE"
        emoji = "✅" if trade.pnl >= 0 else "❌"
        tg_msg = (
            f"{emoji} {trade.symbol} closed (manual) [{mode_tag}]\n"
            f"Entry: {trade.entry_price}  Exit: {exit_price}\n"
            f"PnL: {pnl_sign}${trade.pnl:.2f} ({pnl_sign}{trade.pnl_pct:.2f}%)"
        )
        await notifier.send(tg_msg, parse_mode=None)
    except Exception as exc:
        logger.warning("Manual close Telegram notification failed: %s", exc)

    return TradeResponse.model_validate(trade)


@router.delete("/trades/{trade_id}")
@router.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a single trade record by ID (refuses to delete an open live position)."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status == "open" and not trade.is_paper:
        raise HTTPException(
            status_code=400,
            detail="禁止直接删除持仓中的实盘记录（会导致交易所仓位脱离监控），请先点击平仓！",
        )

    if trade.status == "open":
        bots = _get_bots(request)
        target_bot = bots.get("paper")
        if target_bot is not None and hasattr(target_bot, "positions"):
            pos = target_bot.positions.get(trade.symbol)
            pos_id = pos["id"] if isinstance(pos, dict) else getattr(pos, "id", None)
            if pos_id == trade_id:
                target_bot.positions.pop(trade.symbol, None)

    db.delete(trade)
    db.commit()
    return {"status": "deleted", "id": trade_id}


@router.delete("/trades")
@router.post("/trades/clear")
def clear_trades(request: Request, db: Session = Depends(get_db)):
    """Delete trade records (refuses to clear while any live position is still open)."""
    open_live = db.query(Trade).filter(Trade.status == "open", Trade.is_paper == False).count()  # noqa: E712
    if open_live > 0:
        raise HTTPException(
            status_code=400,
            detail=f"当前仍有 {open_live} 笔持仓中的实盘订单，禁止清空全部记录，请先平仓！",
        )

    deleted_count = db.query(Trade).delete()
    db.commit()

    bots = _get_bots(request)
    for bot in bots.values():
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
    """Return OHLCV + MA7/MA25/MA60 data for charting."""
    try:
        symbol = validate_symbol_str(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"不支持的时间框架: {timeframe}")

    config_mgr = getattr(request.app.state, "config", None)
    cfg = config_mgr.get_all_cached() if (config_mgr is not None and hasattr(config_mgr, "get_all_cached")) else {}

    exchange = _get_cached_public_exchange(cfg.get("http_proxy"))

    raw = []
    if exchange is not None and not endTime:
        def _fetch_ccxt_locked():
            with _public_exchange_call_lock:
                return exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 60)

        try:
            raw = await asyncio.to_thread(_fetch_ccxt_locked)
        except Exception as exc:
            logger.warning("fetch_ohlcv for %s failed: %s", symbol, exc)

    if not raw:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        fetch_limit = limit if endTime else (limit + 60)
        end_time_param = f"&endTime={endTime}" if endTime else ""
        endpoints = [
            f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}&interval={timeframe}&limit={fetch_limit}{end_time_param}",
            f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={fetch_limit}{end_time_param}",
            f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={fetch_limit}{end_time_param}",
        ]
        proxy = _normalize_httpx_proxy(cfg.get("http_proxy"))

        def _fetch_http():
            client_kwargs = {"timeout": 6.0}
            if proxy:
                client_kwargs["proxy"] = proxy
            with httpx.Client(**client_kwargs) as client:
                for endpoint in endpoints:
                    try:
                        resp = client.get(endpoint)
                        if resp.status_code == 200:
                            items = resp.json()
                            if items and isinstance(items, list):
                                return [
                                    [row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
                                    for row in items
                                ]
                    except Exception as e:
                        logger.warning("Failed to fetch live candles from %s: %s", endpoint, e)
            return []

        raw = await asyncio.to_thread(_fetch_http)

    if not raw:
        logger.error("Failed to fetch real live candles for %s from all Binance endpoints.", symbol)
        raise HTTPException(
            status_code=503,
            detail=f"无法获取 {symbol} 的实时真实K线数据，请检查网络连接或全局设置中的代理配置。",
        )

    closes = [b[4] for b in raw]
    n = len(closes)

    def _ma(period: int) -> list:
        result = [None] * n
        if n < period:
            return result
        window_sum = sum(closes[:period])
        result[period - 1] = round(window_sum / period, 4)
        for i in range(period, n):
            window_sum += closes[i] - closes[i - period]
            result[i] = round(window_sum / period, 4)
        return result

    ma7 = _ma(7)
    ma25 = _ma(25)
    ma60 = _ma(60)

    data = [
        {
            "time": bar[0],
            "open": bar[1],
            "high": bar[2],
            "low": bar[3],
            "close": bar[4],
            "volume": bar[5],
            "ma7": ma7[i],
            "ma25": ma25[i],
            "ma60": ma60[i],
        }
        for i, bar in enumerate(raw)
    ]

    return {"symbol": symbol, "timeframe": timeframe, "data": data if endTime else data[-limit:]}


# ---------------------------------------------------------------------------
# Operation Logs
# ---------------------------------------------------------------------------

@router.get("/logs", response_model=OperationLogPageResponse)
def list_operation_logs(
    log_type: Optional[str] = Query(None, description="Filter by log_type: open, close, start, stop"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return paginated persistent operation logs ordered newest first."""
    q = db.query(OperationLog)
    if log_type:
        q = q.filter(OperationLog.log_type == log_type)
    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    items = (
        q.order_by(OperationLog.id.desc())
        .offset((current_page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return OperationLogPageResponse(
        items=items,
        total=total,
        page=current_page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.delete("/logs")
@router.post("/logs/clear")
def clear_operation_logs(db: Session = Depends(get_db)):
    """Clear all operation logs."""
    deleted = db.query(OperationLog).delete()
    db.commit()
    return {"status": "cleared", "deleted": deleted}


# ---------------------------------------------------------------------------
# System Logs (persistent homepage system/signal logs with keyword search)
# ---------------------------------------------------------------------------

@router.get("/system-logs", response_model=SystemLogPageResponse)
def list_system_logs(
    keyword: Optional[str] = Query(None, max_length=100, description="按关键字模糊筛选日志内容或交易对"),
    level: Optional[str] = Query(None, description="按级别筛选: info, success, warning, error"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return paginated persistent system logs ordered newest first, with keyword & level filters."""
    q = db.query(SystemLog)
    if level and level.strip():
        q = q.filter(SystemLog.level == level.strip().lower())
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        q = q.filter(
            or_(
                SystemLog.message.ilike(kw),
                SystemLog.symbol.ilike(kw),
            )
        )
    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    items = (
        q.order_by(SystemLog.id.desc())
        .offset((current_page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return SystemLogPageResponse(
        items=items,
        total=total,
        page=current_page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.delete("/system-logs")
@router.post("/system-logs/clear")
def clear_system_logs(db: Session = Depends(get_db)):
    """Clear all persistent system logs."""
    deleted = db.query(SystemLog).delete()
    db.commit()
    return {"status": "cleared", "deleted": deleted}
