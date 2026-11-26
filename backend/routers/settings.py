"""
API routes for settings and symbol configuration.

Endpoints:
    GET    /api/settings           — return all settings (secrets masked)
    PUT    /api/settings           — bulk update settings (skips unchanged masked values)
    GET    /api/symbols            — list all symbol configs
    POST   /api/symbols            — create a symbol config
    PUT    /api/symbols/{id}       — update a symbol config
    DELETE /api/symbols/{id}       — delete a symbol config
"""

from __future__ import annotations

import re
from typing import Dict, List
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import SymbolConfig, get_db
from config import get_all_settings, update_settings
from models import (
    SymbolConfigCreate,
    SymbolConfigResponse,
    SymbolConfigUpdate,
    validate_htf_gt_ltf,
)

router = APIRouter()

SECRET_KEYS = {"binance_key", "binance_secret", "telegram_token"}
ALLOWED_SETTING_KEYS = {
    "binance_key",
    "binance_secret",
    "telegram_token",
    "telegram_chat_id",
    "http_proxy",
    "trading_mode",
    "max_daily_loss",
}
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_MASK_PREFIX = "****"


def _mask_secret(value: str) -> str:
    """Return a masked representation that never reveals the raw secret."""
    if not value:
        return ""
    if len(value) <= 6:
        return _MASK_PREFIX
    return f"{_MASK_PREFIX}{value[-4:]}"


def _is_masked_placeholder(value: str) -> bool:
    """True when the client echoed back our masked placeholder unchanged."""
    s = value.strip()
    return s.startswith(_MASK_PREFIX) or s.startswith("••••")


def _sanitize_proxy_for_display(proxy: str) -> str:
    """Strip credentials from a proxy URL before showing it in UI/error messages."""
    if not proxy:
        return ""
    return re.sub(r"://[^/@\s]+@", "://***@", proxy)


def _mask_settings_response(raw: Dict[str, str]) -> Dict[str, str]:
    masked = dict(raw)
    for k in SECRET_KEYS:
        if k in masked:
            masked[k] = _mask_secret(masked[k])
    return masked


def _validate_settings_updates(updates: Dict[str, str]) -> Dict[str, str]:
    cleaned: Dict[str, str] = {}
    for k, v in updates.items():
        if k not in ALLOWED_SETTING_KEYS:
            continue
        val = str(v).strip()
        if k in SECRET_KEYS and _is_masked_placeholder(val):
            # Client did not edit this secret field; keep the stored DB value.
            continue
        if k == "http_proxy" and val:
            parsed = urlparse(val)
            if parsed.scheme.lower() not in ALLOWED_PROXY_SCHEMES or not parsed.hostname:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="代理地址格式无效，仅支持 http/https/socks5/socks5h://host:port",
                )
        if k == "max_daily_loss" and val:
            try:
                num = float(val)
                if num < 0 or num > 100:
                    raise ValueError()
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="每日最大亏损比例必须在 0 到 100 之间",
                )
        if k == "trading_mode" and val and val not in {"paper", "live"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="trading_mode 仅支持 paper 或 live",
            )
        cleaned[k] = val
    return cleaned


def _hot_reload_bot_settings(request: Request, full_settings: Dict[str, str]) -> None:
    """Propagate updated notifier / proxy / API credentials to running bots & shared WS."""
    bots = getattr(request.app.state, "bots", {}) or {}
    tg_token = full_settings.get("telegram_token", "")
    tg_chat = full_settings.get("telegram_chat_id", "")
    proxy = full_settings.get("http_proxy", "")
    binance_key = full_settings.get("binance_key", "")
    binance_secret = full_settings.get("binance_secret", "")

    shared_ws = getattr(request.app.state, "ws_streamer", None)
    if shared_ws is not None and hasattr(shared_ws, "update_proxy"):
        shared_ws.update_proxy(proxy)

    for mode_key, bot in bots.items():
        if bot is None:
            continue
        notifier = getattr(bot, "notifier", None)
        if notifier is not None and hasattr(notifier, "update_config"):
            notifier.update_config(tg_token, tg_chat, proxy=proxy)
        data_feed = getattr(bot, "data_feed", None)
        if data_feed is not None:
            if hasattr(data_feed, "update_credentials") and mode_key == "live":
                data_feed.update_credentials(binance_key, binance_secret, proxy=proxy)
            elif hasattr(data_feed, "update_proxy"):
                data_feed.update_proxy(proxy)
        if mode_key == "live" and getattr(bot, "executor", None) is not None and binance_key and binance_secret:
            try:
                from routers.trading import _build_exchange
                new_ex = _build_exchange(full_settings, require_auth=True)
                if new_ex is not None:
                    bot.executor.exchange = new_ex
                    bot.executor.config = full_settings
            except Exception:
                pass

    # Invalidate cached public kline exchange so new proxy takes effect immediately
    try:
        from routers.trading import invalidate_public_exchange_cache
        invalidate_public_exchange_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------

@router.get("/settings", summary="Get all settings (secrets masked)")
def read_settings(db: Session = Depends(get_db)):
    all_settings = get_all_settings(db)
    masked = _mask_settings_response(all_settings)
    resp = dict(masked)
    resp["settings"] = masked
    return resp


@router.put("/settings", summary="Bulk update settings")
@router.post("/settings", summary="Bulk update settings (POST fallback for Nginx/CDN)")
def write_settings(body: dict, request: Request, db: Session = Depends(get_db)):
    if "settings" in body and isinstance(body["settings"], dict):
        raw_updates = {str(k): str(v) for k, v in body["settings"].items()}
    else:
        raw_updates = {str(k): str(v) for k, v in body.items() if k != "settings"}

    updates = _validate_settings_updates(raw_updates)
    updated = update_settings(db, updates) if updates else get_all_settings(db)

    cfg_mgr = getattr(request.app.state, "config", None)
    if cfg_mgr is not None and hasattr(cfg_mgr, "invalidate"):
        cfg_mgr.invalidate()

    _hot_reload_bot_settings(request, updated)

    masked = _mask_settings_response(updated)
    resp = dict(masked)
    resp["settings"] = masked
    return resp


# ---------------------------------------------------------------------------
# Symbol config routes
# ---------------------------------------------------------------------------

@router.get("/symbols", response_model=List[SymbolConfigResponse], summary="List symbol configs")
def list_symbols(db: Session = Depends(get_db)):
    return db.query(SymbolConfig).all()


async def _reload_running_bots(request: Request) -> None:
    bots = getattr(request.app.state, "bots", {}) or {}
    for bot in bots.values():
        if bot is not None and getattr(bot, "running", False) and hasattr(bot, "reload_symbols"):
            await bot.reload_symbols()


@router.post(
    "/symbols",
    response_model=SymbolConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create symbol config",
)
async def create_symbol(body: SymbolConfigCreate, request: Request, db: Session = Depends(get_db)):
    existing = db.query(SymbolConfig).filter(SymbolConfig.symbol == body.symbol.upper()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Symbol '{body.symbol}' already exists.",
        )
    obj = SymbolConfig(
        symbol=body.symbol.upper(),
        timeframe=body.timeframe,
        htf_timeframe=body.htf_timeframe,
        confirm_candles=body.confirm_candles,
        leverage=body.leverage,
        risk_per_trade=body.risk_per_trade,
        rr_ratio=body.rr_ratio,
        trailing_activate_r=body.trailing_activate_r,
        breakeven_buffer=body.breakeven_buffer,
        enabled=body.enabled,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    await _reload_running_bots(request)
    return obj


@router.put("/symbols/{symbol_id}", response_model=SymbolConfigResponse, summary="Update symbol config")
@router.post("/symbols/{symbol_id}", response_model=SymbolConfigResponse, summary="Update symbol config (POST fallback)")
async def update_symbol(symbol_id: int, body: SymbolConfigUpdate, request: Request, db: Session = Depends(get_db)):
    obj = db.query(SymbolConfig).filter(SymbolConfig.id == symbol_id).first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol config not found.")

    update_data = body.model_dump(exclude_unset=True)
    effective_tf = update_data.get("timeframe", obj.timeframe)
    effective_htf = update_data.get("htf_timeframe", obj.htf_timeframe)
    try:
        validate_htf_gt_ltf(effective_tf, effective_htf)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    for field, value in update_data.items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)

    await _reload_running_bots(request)
    return obj


@router.delete(
    "/symbols/{symbol_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete symbol config",
)
async def delete_symbol(symbol_id: int, request: Request, db: Session = Depends(get_db)):
    obj = db.query(SymbolConfig).filter(SymbolConfig.id == symbol_id).first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol config not found.")
    db.delete(obj)
    db.commit()

    await _reload_running_bots(request)


# ---------------------------------------------------------------------------
# Telegram Test Route
# ---------------------------------------------------------------------------

@router.post("/settings/test-telegram", summary="Test Telegram notification")
async def test_telegram_notification(request: Request, db: Session = Depends(get_db)):
    from notifier import TelegramNotifier
    from config import get_setting, set_setting

    token = (get_setting(db, "telegram_token", "") or "").strip()
    chat_id = (get_setting(db, "telegram_chat_id", "") or "").strip()
    proxy = (get_setting(db, "http_proxy", "") or "").strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先在全局设置中填写 Telegram Token！",
        )

    notifier = TelegramNotifier(token=token, chat_id=chat_id, proxy=proxy or None)

    is_numeric_chat_id = chat_id and (chat_id.isdigit() or (chat_id.startswith("-") and chat_id[1:].isdigit()))
    if not is_numeric_chat_id:
        discovered = await notifier.get_latest_chat_id()
        if discovered and discovered.get("chat_id"):
            chat_id = discovered["chat_id"]
            set_setting(db, "telegram_chat_id", chat_id)
            cfg_mgr = getattr(request.app.state, "config", None)
            if cfg_mgr is not None and hasattr(cfg_mgr, "invalidate"):
                cfg_mgr.invalidate()
            notifier.chat_id = chat_id
            notifier._enabled = True
        else:
            err_suffix = f" (网络/代理响应: {notifier.last_error})" if notifier.last_error else ""
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"未检测到有效的数字 Chat ID！请在 Telegram 中打开您的机器人并发送 /start 或任意消息，然后再次点击测试。{err_suffix}",
            )

    msg = (
        "🚀 *MA Trader 通知系统测试*\n\n"
        "✅ 恭喜！Telegram 交易机器人已成功连接！\n"
        "📈 后续实盘/模拟交易的开仓、平仓、移动追踪止盈止损均会实时推送到此。"
    )
    ok = await notifier.send(msg)
    if not ok:
        err_detail = notifier.last_error or "连接超时或拒绝"
        safe_proxy = _sanitize_proxy_for_display(notifier.proxy or "")
        proxy_info = f"\n[使用的代理]: {safe_proxy}" if safe_proxy else "\n[使用的代理]: 直连 (未配置代理)"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"{err_detail}{proxy_info}\n\n"
                "💡 原因提示: 无法连通 api.telegram.org:443，请检查网络或代理端口是否正确。"
            ),
        )

    return {
        "status": "success",
        "chat_id": chat_id,
        "message": f"测试消息发送成功！(目标 Chat ID: {chat_id})",
    }
