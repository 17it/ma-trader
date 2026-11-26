"""
Binance Public WebSocket Streamer
Subscribes to live Futures tickers (with optional Spot fallback for non-live consumers),
caches prices in memory along with source type (futures vs spot), and invokes callbacks.
"""

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set

import websockets

sys.modules.setdefault("binance_ws", sys.modules[__name__])
sys.modules.setdefault("backend.binance_ws", sys.modules[__name__])

logger = logging.getLogger(__name__)

FUTURES_WS_URL = "wss://fstream.binance.com/stream"
PRIMARY_SPOT_WS_URL = "wss://data-stream.binance.vision/stream"
FALLBACK_SPOT_WS_URL = "wss://stream.binance.com:9443/stream"

_WS_SUPPORTS_PROXY = "proxy" in inspect.signature(websockets.connect).parameters
try:
    import python_socks  # noqa: F401
    _HAS_PYTHON_SOCKS = True
except ImportError:
    _HAS_PYTHON_SOCKS = False
_WARNED_MISSING_SOCKS = False


def _normalize_ws_proxy(proxy: Optional[str]) -> Optional[str]:
    global _WARNED_MISSING_SOCKS
    p = (proxy or "").strip()
    if not p:
        p = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    if not p:
        return None
    if p.startswith("socks5h://"):
        p = "socks5://" + p[len("socks5h://"):]
    if p.lower().startswith("socks") and not _HAS_PYTHON_SOCKS:
        if not _WARNED_MISSING_SOCKS:
            logger.error(
                "BinanceWSStreamer: SOCKS proxy configured (%s) but 'python-socks[asyncio]' is not installed; "
                "run 'pip install python-socks[asyncio]' to enable WebSocket SOCKS proxy.",
                p.split("@")[-1],
            )
            _WARNED_MISSING_SOCKS = True
        return None
    return p


class BinanceWSStreamer:
    """
    Subscribes to Binance public WebSocket streams for live tickers.
    Maintains an in-memory price cache and tracks whether the active stream is
    Futures or Spot so live Futures bots never silently consume Spot ticks.
    """

    def __init__(self, use_futures: bool = True, proxy: Optional[str] = None):
        self.symbols: Set[str] = set()
        self.use_futures = use_futures
        self._proxy: Optional[str] = _normalize_ws_proxy(proxy)
        self.latest_prices: Dict[str, float] = {}
        self.latest_ts: Dict[str, float] = {}
        self.latest_is_futures: Dict[str, bool] = {}
        self.callbacks: List[Callable[[dict], Any]] = []
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._active_is_futures: bool = use_futures

    def update_proxy(self, proxy: Optional[str]) -> None:
        """Hot-reload proxy setting and reconnect if changed while running."""
        new_proxy = _normalize_ws_proxy(proxy)
        if new_proxy != self._proxy:
            self._proxy = new_proxy
            if self._running and self._task and not self._task.done():
                self._task.cancel()
                self._task = asyncio.create_task(self._stream_loop(), name="binance-ws-streamer")

    def register_callback(self, cb: Callable[[dict], Any]):
        """Register a callback to be called on each ticker update."""
        self.callbacks.append(cb)

    def get_cached_price(
        self,
        symbol: str,
        max_age: float = 15.0,
        require_futures: bool = False,
    ) -> Optional[float]:
        """
        Return the cached price if available and fresh within max_age seconds.
        When require_futures=True, returns None if the cached tick came from a Spot stream.
        """
        clean = symbol.replace("/", "").replace(":USDT", "").upper()
        ts = self.latest_ts.get(clean)
        if ts is not None and (time.time() - ts) <= max_age:
            if require_futures and not self.latest_is_futures.get(clean, False):
                return None
            return self.latest_prices.get(clean)
        return None

    def start(self, symbols: List[str]):
        """Start the WebSocket streamer background task."""
        cleaned = {s.replace("/", "").replace(":USDT", "").upper() for s in symbols if s}
        self.symbols = cleaned
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._stream_loop(), name="binance-ws-streamer")
        logger.info("BinanceWSStreamer started for symbols: %s", list(self.symbols))

    def update_symbols(self, symbols: List[str]):
        """Update subscribed symbols; restarts stream if running."""
        cleaned = {s.replace("/", "").replace(":USDT", "").upper() for s in symbols if s}
        if cleaned != self.symbols:
            self.symbols = cleaned
            if self._running and self._task and not self._task.done():
                self._task.cancel()
                self._task = asyncio.create_task(self._stream_loop(), name="binance-ws-streamer")
                logger.info("BinanceWSStreamer updated symbols: %s", list(self.symbols))

    async def stop(self):
        """Stop the streamer."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("BinanceWSStreamer stopped")

    async def _stream_loop(self):
        """
        Main connection loop with automatic reconnect.
        Always tries Futures WS first when use_futures=True, and resets back to
        Futures WS after any successful connection closes so a transient blip
        never leaves the streamer permanently stuck on Spot.
        """
        endpoints = (
            [(FUTURES_WS_URL, True), (PRIMARY_SPOT_WS_URL, False), (FALLBACK_SPOT_WS_URL, False)]
            if self.use_futures
            else [(PRIMARY_SPOT_WS_URL, False), (FALLBACK_SPOT_WS_URL, False)]
        )
        url_idx = 0
        backoff = 1.0

        while self._running:
            if not self.symbols:
                await asyncio.sleep(2.0)
                continue

            stream_names = [f"{s.lower()}@ticker" for s in sorted(self.symbols)]
            base_url, is_futures = endpoints[url_idx % len(endpoints)]
            url = f"{base_url}?streams={'/'.join(stream_names)}"

            try:
                logger.info(
                    "Connecting to Binance WebSocket: %s (futures=%s, %d streams)",
                    base_url, is_futures, len(stream_names),
                )
                connect_kwargs: Dict[str, Any] = {
                    "ping_interval": 20,
                    "ping_timeout": 15,
                    "close_timeout": 5,
                }
                if _WS_SUPPORTS_PROXY and self._proxy:
                    connect_kwargs["proxy"] = self._proxy

                async with websockets.connect(url, **connect_kwargs) as ws:
                    self._active_is_futures = is_futures
                    logger.info("Connected to Binance WebSocket (futures=%s)", is_futures)
                    backoff = 1.0
                    # If connected to primary futures stream, keep trying futures first next time too
                    if is_futures:
                        url_idx = 0

                    while self._running:
                        msg_raw = await ws.recv()
                        try:
                            msg = json.loads(msg_raw)
                            data = msg.get("data") if isinstance(msg, dict) else None
                            if not data or not isinstance(data, dict):
                                continue

                            s = data.get("s")
                            c = data.get("c")
                            if s and c:
                                price = float(c)
                                self.latest_prices[s] = price
                                self.latest_ts[s] = time.time()
                                self.latest_is_futures[s] = is_futures

                                payload = {"type": "ticker", "symbol": s, "price": price}
                                for cb in self.callbacks:
                                    try:
                                        res = cb(payload)
                                        if asyncio.iscoroutine(res):
                                            asyncio.create_task(res)
                                    except Exception:
                                        pass
                        except Exception as e:
                            logger.debug("Error processing WS frame: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Binance WebSocket (%s) disconnected: %s. Reconnecting in %.1fs...",
                    base_url, exc, backoff,
                )
                url_idx = (url_idx + 1) % len(endpoints)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)
