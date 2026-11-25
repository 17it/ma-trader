"""
MA Trader - Telegram Notification Module
Sends trade alerts via the Telegram Bot API using httpx (unified HTTP client).
Silently fails when token / chat_id are not configured, and redacts secrets from logs.
"""
import asyncio
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TOKEN_RE = re.compile(r"/bot[^/\s]+")
_PROXY_CRED_RE = re.compile(r"://[^/@\s]+@")


def _redact(text: str) -> str:
    """Strip Telegram bot tokens and proxy credentials from error/log strings."""
    if not text:
        return ""
    text = _TOKEN_RE.sub("/bot<REDACTED>", str(text))
    text = _PROXY_CRED_RE.sub("://***@", text)
    return text


def _get_configured_proxy() -> Optional[str]:
    """Retrieve user-configured proxy from database settings or environment."""
    proxy = ""
    try:
        from database import SessionLocal, SettingModel
        db = SessionLocal()
        try:
            setting = db.query(SettingModel).filter(SettingModel.key == "http_proxy").first()
            if setting and setting.value and setting.value.strip():
                proxy = setting.value.strip()
        finally:
            db.close()
    except Exception:
        pass

    if not proxy:
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
            or ""
        ).strip()

    return proxy or None


def _normalize_httpx_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith("socks5h://"):
        return "socks5://" + p[len("socks5h://"):]
    return p


class TelegramNotifier:
    """
    Async Telegram notifier with proxy support from global settings.

    All public methods silently swallow errors when the bot is not
    configured, so callers never need to guard against missing credentials.
    """

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, proxy: Optional[str] = None):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.proxy = _normalize_httpx_proxy(proxy if proxy is not None else _get_configured_proxy())
        self.last_error: Optional[str] = None
        self._enabled = bool(self.token and self.chat_id)
        if not self._enabled:
            logger.info("TelegramNotifier: token/chat_id not configured — notifications disabled.")

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    async def send(self, message: str, parse_mode: Optional[str] = "Markdown") -> bool:
        """
        Send a message to the configured chat.
        Uses Markdown by default and automatically falls back to plain text if
        Telegram rejects unescaped Markdown characters (HTTP 400).
        """
        if not self._enabled:
            return False
        return await asyncio.to_thread(self._send_sync, message, parse_mode)

    def _send_sync(self, message: str, parse_mode: Optional[str] = "Markdown") -> bool:
        url = TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        client_kwargs = {"timeout": 12.0}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy

        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 400 and parse_mode:
                    # Retry without Markdown formatting if message contains special chars
                    payload.pop("parse_mode", None)
                    resp = client.post(url, json=payload)
                resp.raise_for_status()
            self.last_error = None
            return True
        except Exception as exc:
            safe_err = _redact(str(exc))
            self.last_error = safe_err
            logger.warning("Telegram send failed (proxy=%s): %s", _redact(self.proxy or "direct"), safe_err)
            return False

    async def get_latest_chat_id(self) -> Optional[dict]:
        """Fetch the most recent incoming message from getUpdates to discover chat_id."""
        if not self.token:
            return None
        return await asyncio.to_thread(self._get_latest_chat_id_sync)

    def _get_latest_chat_id_sync(self) -> Optional[dict]:
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        client_kwargs = {"timeout": 10.0}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy
        try:
            with httpx.Client(**client_kwargs) as client:
                r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                updates = data.get("result", [])
                if updates:
                    last = updates[-1]
                    msg = last.get("message") or last.get("channel_post") or {}
                    chat = msg.get("chat") or {}
                    user = msg.get("from") or {}
                    if chat.get("id") is not None:
                        return {
                            "chat_id": str(chat.get("id")),
                            "username": user.get("username") or chat.get("username"),
                            "first_name": user.get("first_name") or chat.get("title"),
                            "text": msg.get("text"),
                        }
        except Exception as exc:
            safe_err = _redact(str(exc))
            self.last_error = safe_err
            logger.warning("Telegram getUpdates failed (proxy=%s): %s", _redact(self.proxy or "direct"), safe_err)
        return None

    # ------------------------------------------------------------------
    # Trade notifications
    # ------------------------------------------------------------------

    async def notify_trade_open(self, trade: dict) -> None:
        symbol = trade.get("symbol", "?")
        direction = trade.get("direction", "?")
        entry = trade.get("entry_price", 0)
        sl = trade.get("stop_loss", 0)
        tp = trade.get("take_profit", 0)
        tf = trade.get("timeframe", "?")
        confirms = trade.get("confirm_count", 0)
        is_paper = trade.get("is_paper", False)

        emoji = "🟢" if direction == "long" else "🔴"
        dir_cn = "开多仓" if direction == "long" else "开空仓"
        paper_tag = " `[模拟]`" if is_paper else " `[实盘]`"

        msg = (
            f"{emoji} *{dir_cn} {symbol}*{paper_tag}\n"
            f"入场: `{entry:,.4f}` | 止损: `{sl:,.4f}` | 止盈: `{tp:,.4f}`\n"
            f"时间框架: `{tf}` | 确认: `{confirms}次`"
        )
        await self.send(msg)

    async def notify_trade_close(self, trade: dict) -> None:
        symbol = trade.get("symbol", "?")
        direction = trade.get("direction", "?")
        pnl = trade.get("pnl", 0) or 0
        pnl_pct = trade.get("pnl_pct", 0) or 0
        reason_map = {
            "take_profit": "止盈",
            "stop_loss": "初始止损",
            "profit_lock_stop": "阶梯锁盈",
            "trailing_stop": "移动止损",
            "ma7_trailing": "MA7 止盈",
            "ma60_cross": "MA60 穿线",
            "exchange_closed": "交易所已平仓",
            "manual": "手动平仓",
            "signal": "信号反转",
        }
        reason = reason_map.get(trade.get("close_reason", ""), trade.get("close_reason", "?"))
        is_paper = trade.get("is_paper", False)

        emoji = "🟩" if pnl >= 0 else "🟥"
        pnl_sign = "+" if pnl >= 0 else ""
        paper_tag = " `[模拟]`" if is_paper else " `[实盘]`"

        msg = (
            f"{emoji} *平仓 {symbol}* ({reason}){paper_tag}\n"
            f"方向: `{direction}` | "
            f"盈亏: `{pnl_sign}${pnl:.2f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
        )
        await self.send(msg)

    async def notify_signal(self, symbol: str, direction: str, confirm: int, max_confirm: int) -> None:
        dir_cn = "多头" if direction == "long" else "空头"
        bar = "▓" * confirm + "░" * max(0, max_confirm - confirm)
        msg = f"⚠️ *信号确认 {symbol} {dir_cn}* `{confirm}/{max_confirm}`\n`[{bar}]`"
        await self.send(msg)

    async def notify_error(self, error_msg: str) -> None:
        msg = f"🚨 *系统错误*\n`{_redact(error_msg)}`"
        await self.send(msg)

    async def notify_bot_started(self) -> None:
        await self.send("🤖 *MA Trader 已启动*")

    async def notify_bot_stopped(self) -> None:
        await self.send("⏹ *MA Trader 已停止*")

    # ------------------------------------------------------------------
    # Configuration update
    # ------------------------------------------------------------------

    def update_config(
        self,
        token: Optional[str],
        chat_id: Optional[str],
        proxy: Optional[str] = None,
    ) -> None:
        """Hot-reload credentials and proxy without restarting."""
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        if proxy is not None:
            self.proxy = _normalize_httpx_proxy(proxy.strip() or None)
        self._enabled = bool(self.token and self.chat_id)
        logger.info("TelegramNotifier config updated. Enabled: %s", self._enabled)
