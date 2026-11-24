"""
Binance Public WebSocket Streamer
Subscribes to live tickers via Binance Vision public stream (no auth, no geo-block).
Caches prices in memory and invokes callbacks for real-time broadcasts.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

import websockets

logger = logging.getLogger(__name__)

PRIMARY_WS_URL = "wss://data-stream.binance.vision/stream"
FALLBACK_WS_URL = "wss://stream.binance.com:9443/stream"


class BinanceWSStreamer:
    """
    Subscribes to Binance public WebSocket streams for live tickers.
    Maintains an in-memory price cache for instant zero-HTTP lookups.
    """

    def __init__(self):
        self.symbols: Set[str] = set()
        self.latest_prices: Dict[str, float] = {}
        self.latest_ts: Dict[str, float] = {}
        self.callbacks: List[Callable[[dict], Any]] = []
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    def register_callback(self, cb: Callable[[dict], Any]):
        """Register a callback to be called on each ticker update."""
        self.callbacks.append(cb)

    def get_cached_price(self, symbol: str, max_age: float = 15.0) -> Optional[float]:
        """
        Return the cached price if available and fresh within max_age seconds.
        Returns None if expired or not found.
        """
        clean = symbol.replace("/", "").replace(":USDT", "").upper()
        ts = self.latest_ts.get(clean)
        if ts is not None and (time.time() - ts) <= max_age:
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
        """Main connection and message dispatch loop with automatic reconnect."""
        urls = [PRIMARY_WS_URL, FALLBACK_WS_URL]
        url_idx = 0
        backoff = 1.0

        while self._running:
            if not self.symbols:
                await asyncio.sleep(2.0)
                continue

            stream_names = [f"{s.lower()}@ticker" for s in sorted(self.symbols)]
            base_url = urls[url_idx % len(urls)]
            url = f"{base_url}?streams={'/'.join(stream_names)}"

            try:
                logger.info("Connecting to Binance WebSocket: %s (%d streams)", base_url, len(stream_names))
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=5,
                ) as ws:
                    logger.info("Connected to Binance WebSocket market data stream")
                    backoff = 1.0  # Reset backoff on successful connect

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
                    "Binance WebSocket disconnected: %s. Reconnecting in %.1fs...",
                    exc,
                    backoff,
                )
                url_idx += 1  # Try next URL on error
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)
