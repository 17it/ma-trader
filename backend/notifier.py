"""
MA Trader - Telegram Notification Module
Sends trade alerts via the Telegram Bot API.
Silently fails when token / chat_id are not configured.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _get_notifier_proxy() -> Optional[str]:
    """Auto-detect proxy for Telegram API requests, preferring local xray socks5h."""
    # Test local xray proxy first (socks5h ensures remote DNS resolution)
    for candidate in ["socks5h://127.0.0.1:10808", "http://127.0.0.1:10808"]:
        try:
            import socket
            s = socket.socket()
            s.settimeout(0.2)
            s.connect(("127.0.0.1", 10808))
            s.close()
            return candidate
        except Exception:
            pass

    import os
    env_p = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")
    if env_p:
        if env_p.startswith("socks5://"):
            return env_p.replace("socks5://", "socks5h://")
        return env_p
    return None


class TelegramNotifier:
    """
    Async Telegram notifier with robust remote DNS socks5h proxy support.

    All public methods silently swallow errors when the bot is not
    configured, so callers never need to guard against missing credentials.
    """

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, proxy: Optional[str] = None):
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.proxy = proxy or _get_notifier_proxy()
        self._enabled = bool(self.token and self.chat_id)
        if not self._enabled:
            logger.info("TelegramNotifier: token/chat_id not configured — notifications disabled.")

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    async def send(self, message: str) -> bool:
        """
        Send a plain-text (Markdown) message to the configured chat.
        Runs requests in a thread pool for rock-solid socks5h support.
        """
        if not self._enabled:
            return False
        import asyncio
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: str) -> bool:
        import requests
        url = TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}
        try:
            resp = requests.post(url, json=payload, proxies=proxies, timeout=12)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(f"Telegram send failed: {exc}")
            return False

    async def get_latest_chat_id(self) -> Optional[dict]:
        """Fetch the most recent incoming message from getUpdates to discover chat_id."""
        if not self.token:
            return None
        import asyncio
        return await asyncio.to_thread(self._get_latest_chat_id_sync)

    def _get_latest_chat_id_sync(self) -> Optional[dict]:
        import requests
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}
        try:
            r = requests.get(url, proxies=proxies, timeout=10)
            if r.status_code == 200:
                data = r.json()
                updates = data.get("result", [])
                if updates:
                    last = updates[-1]
                    msg = last.get("message") or last.get("channel_post") or {}
                    chat = msg.get("chat") or {}
                    user = msg.get("from") or {}
                    return {
                        "chat_id": str(chat.get("id")),
                        "username": user.get("username") or chat.get("username"),
                        "first_name": user.get("first_name") or chat.get("title"),
                        "text": msg.get("text"),
                    }
        except Exception as exc:
            logger.warning(f"Telegram getUpdates failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Trade notifications
    # ------------------------------------------------------------------

    async def notify_trade_open(self, trade: dict) -> None:
        """
        Notify about a newly opened trade.

        Expected trade keys: symbol, direction, entry_price, stop_loss,
        take_profit, timeframe, confirm_count.
        """
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
        paper_tag = " `[模拟]`" if is_paper else ""

        msg = (
            f"{emoji} *{dir_cn} {symbol}*{paper_tag}\n"
            f"入场: `{entry:,.4f}` | 止损: `{sl:,.4f}` | 止盈: `{tp:,.4f}`\n"
            f"时间框架: `{tf}` | 确认: `{confirms}次`"
        )
        await self.send(msg)

    async def notify_trade_close(self, trade: dict) -> None:
        """
        Notify about a closed trade.

        Expected trade keys: symbol, direction, pnl, pnl_pct, close_reason.
        """
        symbol = trade.get("symbol", "?")
        direction = trade.get("direction", "?")
        pnl = trade.get("pnl", 0) or 0
        pnl_pct = trade.get("pnl_pct", 0) or 0
        reason_map = {
            "take_profit": "止盈",
            "stop_loss": "止损",
            "manual": "手动",
            "signal": "信号反转",
        }
        reason = reason_map.get(trade.get("close_reason", ""), trade.get("close_reason", "?"))
        is_paper = trade.get("is_paper", False)

        emoji = "🟩" if pnl >= 0 else "🟥"
        pnl_sign = "+" if pnl >= 0 else ""
        paper_tag = " `[模拟]`" if is_paper else ""

        msg = (
            f"{emoji} *平仓 {symbol}* ({reason}){paper_tag}\n"
            f"方向: `{direction}` | "
            f"盈亏: `{pnl_sign}${pnl:.2f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
        )
        await self.send(msg)

    async def notify_signal(
        self,
        symbol: str,
        direction: str,
        confirm: int,
        max_confirm: int,
    ) -> None:
        """
        Notify about a signal confirmation in progress.

        Args:
            symbol:      Trading pair, e.g. "BTCUSDT".
            direction:   "long" or "short".
            confirm:     Current confirmation count.
            max_confirm: Required confirmation count to enter.
        """
        dir_cn = "多头" if direction == "long" else "空头"
        bar = "▓" * confirm + "░" * (max_confirm - confirm)
        msg = (
            f"⚠️ *信号确认 {symbol} {dir_cn}* `{confirm}/{max_confirm}`\n"
            f"`[{bar}]`"
        )
        await self.send(msg)

    async def notify_error(self, error_msg: str) -> None:
        """Send a system error notification."""
        msg = f"🚨 *系统错误*\n`{error_msg}`"
        await self.send(msg)

    async def notify_bot_started(self) -> None:
        await self.send("🤖 *MA Trader 已启动*")

    async def notify_bot_stopped(self) -> None:
        await self.send("⏹ *MA Trader 已停止*")

    # ------------------------------------------------------------------
    # Configuration update
    # ------------------------------------------------------------------

    def update_config(self, token: Optional[str], chat_id: Optional[str]) -> None:
        """Hot-reload credentials without restarting."""
        self.token = token or ""
        self.chat_id = chat_id or ""
        self._enabled = bool(self.token and self.chat_id)
        logger.info(f"TelegramNotifier config updated. Enabled: {self._enabled}")
