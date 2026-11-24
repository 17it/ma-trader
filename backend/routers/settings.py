"""
API routes for settings and symbol configuration.

Endpoints:
    GET    /api/settings           — return all settings as dict
    PUT    /api/settings           — bulk update settings
    GET    /api/symbols            — list all symbol configs
    POST   /api/symbols            — create a symbol config
    PUT    /api/symbols/{id}       — update a symbol config
    DELETE /api/symbols/{id}       — delete a symbol config
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import SymbolConfig, get_db
from config import get_all_settings, update_settings
from models import (
    SettingResponse,
    SettingUpdate,
    SymbolConfigCreate,
    SymbolConfigResponse,
    SymbolConfigUpdate,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------

@router.get("/settings", summary="Get all settings")
def read_settings(db: Session = Depends(get_db)):
    all_settings = get_all_settings(db)
    resp = dict(all_settings)
    resp["settings"] = all_settings
    return resp


@router.put("/settings", summary="Bulk update settings")
@router.post("/settings", summary="Bulk update settings (POST fallback for Nginx/CDN)")
def write_settings(body: dict, db: Session = Depends(get_db)):
    # Support both flat JSON object and {"settings": {...}}
    if "settings" in body and isinstance(body["settings"], dict):
        updates = {str(k): str(v) for k, v in body["settings"].items()}
    else:
        updates = {str(k): str(v) for k, v in body.items() if k != "settings"}

    updated = update_settings(db, updates)
    resp = dict(updated)
    resp["settings"] = updated
    return resp


# ---------------------------------------------------------------------------
# Symbol config routes
# ---------------------------------------------------------------------------

@router.get("/symbols", response_model=List[SymbolConfigResponse], summary="List symbol configs")
def list_symbols(db: Session = Depends(get_db)):
    return db.query(SymbolConfig).all()


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
        enabled=body.enabled,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    bot = getattr(request.app.state, "bot", None)
    if bot is not None and bot.running and hasattr(bot, "reload_symbols"):
        await bot.reload_symbols()

    return obj


@router.put("/symbols/{symbol_id}", response_model=SymbolConfigResponse, summary="Update symbol config")
@router.post("/symbols/{symbol_id}", response_model=SymbolConfigResponse, summary="Update symbol config (POST fallback)")
async def update_symbol(symbol_id: int, body: SymbolConfigUpdate, request: Request, db: Session = Depends(get_db)):
    obj = db.query(SymbolConfig).filter(SymbolConfig.id == symbol_id).first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol config not found.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)

    bot = getattr(request.app.state, "bot", None)
    if bot is not None and bot.running and hasattr(bot, "reload_symbols"):
        await bot.reload_symbols()

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

    bot = getattr(request.app.state, "bot", None)
    if bot is not None and bot.running and hasattr(bot, "reload_symbols"):
        await bot.reload_symbols()


# ---------------------------------------------------------------------------
# Telegram Test Route
# ---------------------------------------------------------------------------

@router.post("/settings/test-telegram", summary="Test Telegram notification")
async def test_telegram_notification(db: Session = Depends(get_db)):
    from notifier import TelegramNotifier
    from config import get_setting, set_setting

    token = (get_setting(db, "telegram_token", "") or "").strip()
    chat_id = (get_setting(db, "telegram_chat_id", "") or "").strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先在全局设置中填写 Telegram Token！"
        )

    notifier = TelegramNotifier(token=token, chat_id=chat_id)

    # If chat_id not configured or is a non-numeric string (e.g. username like gemini_tony_auto_bot)
    is_numeric_chat_id = chat_id and (chat_id.isdigit() or (chat_id.startswith("-") and chat_id[1:].isdigit()))
    if not is_numeric_chat_id:
        discovered = await notifier.get_latest_chat_id()
        if discovered and discovered.get("chat_id"):
            chat_id = discovered["chat_id"]
            set_setting(db, "telegram_chat_id", chat_id)
            db.commit()
            notifier.chat_id = chat_id
            notifier._enabled = True
        else:
            err_suffix = f" (网络/代理响应: {notifier.last_error})" if notifier.last_error else ""
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"未检测到有效的数字 Chat ID！请在 Telegram 中打开您的机器人并发送 /start 或任意消息，然后再次点击测试。{err_suffix}"
            )

    msg = (
        "🚀 *MA Trader 通知系统测试*\n\n"
        "✅ 恭喜！Telegram 交易机器人已成功连接！\n"
        "📈 后续实盘/模拟交易的开仓、平仓、移动追踪止盈止损均会实时推送到此。\n"
        "⏱ 发送时间: 刚刚"
    )
    ok = await notifier.send(msg)
    if not ok:
        err_detail = notifier.last_error or "连接超时或拒绝"
        proxy_info = f"\n[使用的代理]: {notifier.proxy}" if notifier.proxy else "\n[使用的代理]: 直连 (未配置代理)"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"{err_detail}{proxy_info}\n\n"
                f"💡 原因提示: 全局设置里配置的代理无法连通 api.telegram.org:443。\n"
                f"如果使用本地科学上网客户端，请检查端口是否正确 (例如 Xray 本地端口通常为 socks5://127.0.0.1:10808，Clash 为 http://127.0.0.1:7890)。"
            )
        )

    return {
        "status": "success",
        "chat_id": chat_id,
        "message": f"测试消息发送成功！(目标 Chat ID: {chat_id})"
    }
