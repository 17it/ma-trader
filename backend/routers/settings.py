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

import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from database import SymbolConfig, get_db
from config import PAPER_SCOPED_SETTING_KEYS, extract_env_settings, get_all_settings, update_settings
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
    "strategy_mode",
    "cooldown_bars",
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
        if k == "strategy_mode" and val:
            val = val.lower()
            if val not in {"balanced", "breakout", "classic", "ribbon"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="strategy_mode 仅支持 balanced（方案A）、breakout（方案B）、ribbon（方案C）或 classic（经典）",
                )
        if k == "cooldown_bars" and val != "":
            try:
                cd_num = int(float(val))
                if cd_num < 0 or cd_num > 500:
                    raise ValueError()
                val = str(cd_num)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="平仓冷却 K 线数必须在 0 到 500 之间",
                )
        cleaned[k] = val
    return cleaned


def _hot_reload_bot_settings(request: Request, full_settings: Dict[str, str], env: str = "live") -> None:
    """Propagate updated notifier / proxy / API credentials to matching bots & shared WS."""
    bots = getattr(request.app.state, "bots", {}) or {}
    live_cfg = extract_env_settings(full_settings, "live")
    paper_cfg = extract_env_settings(full_settings, "paper")
    proxy = full_settings.get("http_proxy", "")

    shared_ws = getattr(request.app.state, "ws_streamer", None)
    if shared_ws is not None and hasattr(shared_ws, "update_proxy"):
        shared_ws.update_proxy(proxy)

    for mode_key, bot in bots.items():
        if bot is None:
            continue
        is_paper_bot = mode_key != "live"
        target_cfg = paper_cfg if is_paper_bot else live_cfg
        if hasattr(bot, "update_strategy_settings"):
            bot.update_strategy_settings(
                target_cfg.get("strategy_mode", "balanced"),
                target_cfg.get("cooldown_bars", "30"),
            )
        notifier = getattr(bot, "notifier", None)
        if notifier is not None and hasattr(notifier, "update_config"):
            notifier.update_config(
                target_cfg.get("telegram_token", ""),
                target_cfg.get("telegram_chat_id", ""),
                proxy=proxy,
            )
        data_feed = getattr(bot, "data_feed", None)
        if data_feed is not None:
            if hasattr(data_feed, "update_credentials") and mode_key == "live":
                data_feed.update_credentials(
                    live_cfg.get("binance_key", ""),
                    live_cfg.get("binance_secret", ""),
                    proxy=proxy,
                )
            elif hasattr(data_feed, "update_proxy"):
                data_feed.update_proxy(proxy)
        if (
            mode_key == "live"
            and getattr(bot, "executor", None) is not None
            and live_cfg.get("binance_key")
            and live_cfg.get("binance_secret")
        ):
            try:
                from routers.trading import _build_exchange
                new_ex = _build_exchange(live_cfg, require_auth=True)
                if new_ex is not None:
                    bot.executor.exchange = new_ex
                    bot.executor.config = live_cfg
            except Exception:
                pass

    # Also update shared paper DataFeed if initialized
    shared_feed = getattr(request.app.state, "shared_paper_feed", None)
    if shared_feed is not None and hasattr(shared_feed, "update_proxy"):
        shared_feed.update_proxy(proxy)

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
def read_settings(
    env: str = Query("live", pattern="^(live|paper)$"),
    db: Session = Depends(get_db),
):
    all_settings = get_all_settings(db)
    env_settings = extract_env_settings(all_settings, env)
    masked = _mask_settings_response(env_settings)
    resp = dict(masked)
    resp["settings"] = masked
    return resp


@router.put("/settings", summary="Bulk update settings")
@router.post("/settings", summary="Bulk update settings (POST fallback for Nginx/CDN)")
def write_settings(
    body: dict,
    request: Request,
    env: str = Query("live", pattern="^(live|paper)$"),
    db: Session = Depends(get_db),
):
    if "settings" in body and isinstance(body["settings"], dict):
        raw_updates = {str(k): str(v) for k, v in body["settings"].items()}
    else:
        raw_updates = {str(k): str(v) for k, v in body.items() if k != "settings"}

    updates = _validate_settings_updates(raw_updates)
    if env == "paper":
        paper_updates: Dict[str, str] = {}
        for k, v in updates.items():
            if k in ("binance_key", "binance_secret"):
                continue
            if k in PAPER_SCOPED_SETTING_KEYS:
                paper_updates[f"paper:{k}"] = v
            else:
                paper_updates[k] = v
        updates = paper_updates

    updated = update_settings(db, updates) if updates else get_all_settings(db)

    cfg_mgr = getattr(request.app.state, "config", None)
    if cfg_mgr is not None and hasattr(cfg_mgr, "invalidate"):
        cfg_mgr.invalidate()

    _hot_reload_bot_settings(request, updated, env=env)

    env_view = extract_env_settings(updated, env)
    masked = _mask_settings_response(env_view)
    resp = dict(masked)
    resp["settings"] = masked
    return resp


# ---------------------------------------------------------------------------
# Symbol config routes
# ---------------------------------------------------------------------------

@router.get("/symbols", response_model=List[SymbolConfigResponse], summary="List symbol configs")
def list_symbols(
    is_paper: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    target_paper = False if is_paper is None else bool(is_paper)
    return db.query(SymbolConfig).filter(SymbolConfig.is_paper == target_paper).all()


async def _reload_running_bots(request: Request, db: Optional[Session] = None) -> None:
    bots = getattr(request.app.state, "bots", {}) or {}
    for bot in bots.values():
        if bot is not None and getattr(bot, "running", False) and hasattr(bot, "reload_symbols"):
            await bot.reload_symbols()
    shared_ws = getattr(request.app.state, "ws_streamer", None)
    if shared_ws is not None and getattr(shared_ws, "_running", False) and db is not None:
        rows = db.query(SymbolConfig.symbol).filter_by(enabled=True).distinct().all()
        shared_ws.update_symbols(sorted({r[0] for r in rows if r and r[0]}))


@router.post(
    "/symbols",
    response_model=SymbolConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create symbol config",
)
async def create_symbol(body: SymbolConfigCreate, request: Request, db: Session = Depends(get_db)):
    existing = (
        db.query(SymbolConfig)
        .filter(SymbolConfig.symbol == body.symbol.upper(), SymbolConfig.is_paper == body.is_paper)
        .first()
    )
    if existing:
        env_label = "模拟盘" if body.is_paper else "实盘"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{env_label}已存在交易对 '{body.symbol}'",
        )
    obj = SymbolConfig(
        symbol=body.symbol.upper(),
        is_paper=body.is_paper,
        timeframe=body.timeframe,
        htf_timeframe=body.htf_timeframe,
        confirm_candles=body.confirm_candles,
        leverage=body.leverage,
        risk_per_trade=body.risk_per_trade,
        rr_ratio=body.rr_ratio,
        trailing_activate_r=body.trailing_activate_r,
        breakeven_buffer=body.breakeven_buffer,
        trailing_ma=body.trailing_ma,
        enabled=body.enabled,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    await _reload_running_bots(request, db=db)
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

    await _reload_running_bots(request, db=db)
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

    await _reload_running_bots(request, db=db)


# ---------------------------------------------------------------------------
# Telegram Test Route
# ---------------------------------------------------------------------------

@router.post("/settings/test-telegram", summary="Test Telegram notification")
async def test_telegram_notification(
    request: Request,
    env: str = Query("live", pattern="^(live|paper)$"),
    db: Session = Depends(get_db),
):
    from notifier import TelegramNotifier
    from config import get_setting, set_setting

    token_key = "paper:telegram_token" if env == "paper" else "telegram_token"
    chat_key = "paper:telegram_chat_id" if env == "paper" else "telegram_chat_id"
    token = (get_setting(db, token_key, "") or "").strip()
    chat_id = (get_setting(db, chat_key, "") or "").strip()
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
            set_setting(db, chat_key, chat_id)
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


# ─── Lightweight Server Resource Monitor (/proc + shutil) ─────────────────────

_PROCESS_START_TS = time.time()
_METRICS_LOCK = threading.Lock()
_METRICS_HISTORY: deque = deque(maxlen=60)
_PREV_CPU_TIMES: Optional[Tuple[int, int]] = None
_LAST_SAMPLE_TS: float = 0.0
_LATEST_SNAPSHOT: Optional[dict] = None


def _read_linux_cpu_times() -> Optional[Tuple[int, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline().strip()
        if not first.startswith("cpu "):
            return None
        parts = [int(x) for x in first.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return total, idle
    except Exception:
        return None


def _read_linux_cpu_ghz() -> Optional[float]:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("cpu mhz"):
                    _, val = line.split(":", 1)
                    return round(float(val.strip()) / 1000.0, 2)
    except Exception:
        pass
    return None


def _read_linux_meminfo() -> Dict[str, int]:
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, rest = line.split(":", 1)
                num_str = rest.strip().split()[0]
                if num_str.isdigit():
                    info[k] = int(num_str)
    except Exception:
        pass
    return info


def _read_linux_proc_status() -> Tuple[float, int]:
    rss_mb = 0.0
    threads = threading.active_count()
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = round(int(line.split()[1]) / 1024.0, 2)
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
    except Exception:
        pass
    return rss_mb, threads


def _read_linux_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def _collect_system_metrics_locked() -> dict:
    global _PREV_CPU_TIMES, _LAST_SAMPLE_TS, _LATEST_SNAPSHOT

    now = time.time()
    if _LATEST_SNAPSHOT is not None and (now - _LAST_SAMPLE_TS) < 2.0:
        return _LATEST_SNAPSHOT

    cores = os.cpu_count() or 1
    cpu_ghz = _read_linux_cpu_ghz()

    # CPU %
    cpu_pct = 0.0
    cur_cpu = _read_linux_cpu_times()
    if cur_cpu is not None:
        if _PREV_CPU_TIMES is None:
            _PREV_CPU_TIMES = cur_cpu
            time.sleep(0.05)
            cur_cpu = _read_linux_cpu_times() or cur_cpu
        d_total = cur_cpu[0] - _PREV_CPU_TIMES[0]
        d_idle = cur_cpu[1] - _PREV_CPU_TIMES[1]
        if d_total > 0:
            cpu_pct = max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
        _PREV_CPU_TIMES = cur_cpu
    else:
        try:
            load1 = os.getloadavg()[0]
            cpu_pct = max(0.0, min(100.0, (load1 / cores) * 100.0))
        except Exception:
            cpu_pct = 0.0

    # Memory & Swap
    meminfo = _read_linux_meminfo()
    if meminfo.get("MemTotal"):
        mem_total_kb = meminfo["MemTotal"]
        mem_avail_kb = meminfo.get(
            "MemAvailable",
            meminfo.get("MemFree", 0) + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0),
        )
        mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
        mem_total_mb = round(mem_total_kb / 1024.0, 2)
        mem_used_mb = round(mem_used_kb / 1024.0, 2)
        mem_pct = round((mem_used_kb / mem_total_kb) * 100.0, 1)

        swap_total_kb = meminfo.get("SwapTotal", 0)
        swap_free_kb = meminfo.get("SwapFree", 0)
        swap_used_kb = max(0, swap_total_kb - swap_free_kb)
        swap_total_gb = round(swap_total_kb / (1024.0 * 1024.0), 2)
        swap_used_gb = round(swap_used_kb / (1024.0 * 1024.0), 2)
        swap_pct = round((swap_used_kb / swap_total_kb) * 100.0, 1) if swap_total_kb > 0 else 0.0
    else:
        mem_total_mb = 0.0
        mem_used_mb = 0.0
        mem_pct = 0.0
        swap_total_gb = 0.0
        swap_used_gb = 0.0
        swap_pct = 0.0

    # Disk
    disk_usage = shutil.disk_usage(Path("/"))
    disk_total_gb = round(disk_usage.total / (1024.0 ** 3), 2)
    disk_used_gb = round(disk_usage.used / (1024.0 ** 3), 2)
    disk_free_gb = round(disk_usage.free / (1024.0 ** 3), 2)
    disk_pct = round((disk_usage.used / disk_usage.total) * 100.0, 1) if disk_usage.total > 0 else 0.0

    # Uptime & Process info
    app_uptime_sec = max(0, int(now - _PROCESS_START_TS))
    os_uptime_raw = _read_linux_uptime()
    os_uptime_sec = int(os_uptime_raw) if os_uptime_raw is not None else app_uptime_sec

    proc_rss_mb, proc_threads = _read_linux_proc_status()
    if proc_rss_mb <= 0:
        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0
            proc_rss_mb = round(ru / divisor, 2)
        except Exception:
            proc_rss_mb = 0.0

    point = {
        "t": int(now),
        "cpu": round(cpu_pct, 1),
        "mem": mem_pct,
        "swap": swap_pct,
        "disk": disk_pct,
    }
    _METRICS_HISTORY.append(point)
    _LAST_SAMPLE_TS = now

    hist = list(_METRICS_HISTORY)

    def _agg(key: str) -> dict:
        vals = [p[key] for p in hist]
        return {
            "avg": round(sum(vals) / len(vals), 1) if vals else 0.0,
            "peak": round(max(vals), 1) if vals else 0.0,
        }

    _LATEST_SNAPSHOT = {
        "cpu": {
            "pct": point["cpu"],
            "cores": cores,
            "ghz": cpu_ghz,
            **_agg("cpu"),
        },
        "memory": {
            "pct": mem_pct,
            "used_mb": mem_used_mb,
            "total_mb": mem_total_mb,
            **_agg("mem"),
        },
        "swap": {
            "pct": swap_pct,
            "used_gb": swap_used_gb,
            "total_gb": swap_total_gb,
            **_agg("swap"),
        },
        "disk": {
            "pct": disk_pct,
            "used_gb": disk_used_gb,
            "total_gb": disk_total_gb,
            "free_gb": disk_free_gb,
            **_agg("disk"),
        },
        "runtime": {
            "app_uptime_sec": app_uptime_sec,
            "os_uptime_sec": os_uptime_sec,
            "proc_mem_mb": proc_rss_mb,
            "proc_threads": proc_threads,
        },
        "history": hist,
    }
    return _LATEST_SNAPSHOT


@router.get("/system/metrics", summary="Get lightweight server resource metrics")
def get_system_metrics():
    """Return CPU, memory, swap, disk, uptime, and process metrics from /proc."""
    with _METRICS_LOCK:
        return _collect_system_metrics_locked()
