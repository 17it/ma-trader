"""
Data feed module — Binance OHLCV and account data via ccxt.
Supports both Futures and Spot, and a read-only (no-key) mode.
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class DataFeed:
    """
    Thin wrapper around ccxt for Binance market data and account info.

    - Provide api_key / secret for authenticated (trading) mode.
    - Omit them (or pass None) for read-only market-data mode.
    - use_futures=True (default) connects to Binance USD-M Futures.
      Set to False for Spot.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        use_futures: bool = True,
        proxy: Optional[str] = None,
        ws_streamer: Optional[object] = None,
    ):
        self._ws_streamer = ws_streamer
        try:
            import ccxt, os

            options: dict = {}
            if use_futures:
                options["defaultType"] = "future"

            params = {
                "apiKey": api_key or "",
                "secret": secret or "",
                "options": options,
                "enableRateLimit": True,
                "timeout": 5000,
            }
            active_proxy = (proxy or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
            if active_proxy:
                params["proxies"] = {
                    "http": active_proxy,
                    "https": active_proxy,
                }

            self._exchange = ccxt.binance(params)
            self._authenticated = bool(api_key and secret)
            self._use_futures = use_futures
            self._proxy = active_proxy
            self._geo_blocked = False
            logger.info(
                "DataFeed initialised (futures=%s, auth=%s, proxy=%s)",
                use_futures,
                self._authenticated,
                bool(active_proxy),
            )
        except Exception as exc:
            logger.error("DataFeed init failed: %s", exc)
            self._exchange = None
            self._authenticated = False
            self._proxy = None

    def set_ws_streamer(self, ws_streamer):
        """Attach or update the WebSocket real-time price streamer."""
        self._ws_streamer = ws_streamer

    # ------------------------------------------------------------------
    # Public Binance Vision Fallback (No Geo/451 Restriction)
    # ------------------------------------------------------------------
    def _fetch_fallback_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> Optional[pd.DataFrame]:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        urls = [
            f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}",
            f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}",
        ]
        import httpx
        for url in urls:
            for proxy in [getattr(self, "_proxy", None), None]:
                try:
                    kwargs = {"timeout": 4.0}
                    if proxy:
                        kwargs["proxy"] = proxy
                    resp = httpx.get(url, **kwargs)
                    if resp.status_code == 200:
                        raw = resp.json()
                        if raw and isinstance(raw, list):
                            parsed = []
                            for row in raw:
                                parsed.append([
                                    row[0],
                                    float(row[1]),
                                    float(row[2]),
                                    float(row[3]),
                                    float(row[4]),
                                    float(row[5]),
                                ])
                            df = pd.DataFrame(parsed, columns=["timestamp", "open", "high", "low", "close", "volume"])
                            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                            return df
                except Exception:
                    pass
        return None

    def _fetch_fallback_ticker(self, symbol: str) -> Optional[float]:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        urls = [
            f"https://data-api.binance.vision/api/v3/ticker/price?symbol={clean_symbol}",
            f"https://api.binance.com/api/v3/ticker/price?symbol={clean_symbol}",
        ]
        import httpx
        for url in urls:
            for proxy in [getattr(self, "_proxy", None), None]:
                try:
                    kwargs = {"timeout": 3.0}
                    if proxy:
                        kwargs["proxy"] = proxy
                    resp = httpx.get(url, **kwargs)
                    if resp.status_code == 200:
                        p = float(resp.json().get("price") or 0)
                        if p > 0:
                            return p
                except Exception:
                    pass
        return None

    # ------------------------------------------------------------------
    # OHLCV
    # ------------------------------------------------------------------
    def get_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 200
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV candles for *symbol* on *timeframe*.

        Returns a DataFrame with columns:
            timestamp (UTC datetime), open, high, low, close, volume

        Returns None on failure.
        """
        if self._exchange is not None and not getattr(self, "_geo_blocked", False):
            try:
                raw = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                if raw:
                    df = pd.DataFrame(
                        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                    for col in ("open", "high", "low", "close", "volume"):
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df = df.dropna(subset=["close"])
                    df = df.reset_index(drop=True)
                    return df
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "restricted location" in err_str.lower():
                    if not getattr(self, "_geo_blocked", False):
                        logger.warning(
                            "Binance API (api.binance.com) is geo-restricted (HTTP 451) on this server. "
                            "Switching to public Binance Vision endpoint (data-api.binance.vision) for market data."
                        )
                        self._geo_blocked = True
                else:
                    logger.warning("get_ohlcv ccxt error (%s): %s. Trying public Binance Vision endpoint...", symbol, exc)

        # 1. Fallback to real official Binance Vision market data
        real_df = self._fetch_fallback_ohlcv(symbol, timeframe, limit)
        if real_df is not None:
            return real_df

        # No simulated fallback. Report failure immediately.
        logger.error("Failed to fetch real OHLCV for %s: all Binance endpoints unreachable.", symbol)
        return None

    # ------------------------------------------------------------------
    # Ticker
    # ------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> Optional[float]:
        """
        Return the last traded price for *symbol*, or None on failure.
        Prioritizes the real-time WebSocket cache (0ms, zero HTTP requests).
        Falls back to ccxt / Binance Vision public REST endpoints when cache is empty.
        """
        # 1. Zero-HTTP WebSocket cache
        if getattr(self, "_ws_streamer", None):
            cached = self._ws_streamer.get_cached_price(symbol)
            if cached is not None and cached > 0:
                return cached

        # 2. REST fallback
        if self._exchange is not None and not getattr(self, "_geo_blocked", False):
            try:
                ticker = self._exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or ticker.get("close") or 0)
                if price > 0:
                    return price
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "restricted location" in err_str.lower():
                    if not getattr(self, "_geo_blocked", False):
                        logger.warning(
                            "Binance API (api.binance.com) is geo-restricted (HTTP 451) on this server. "
                            "Switching to public Binance Vision endpoint (data-api.binance.vision) for market data."
                        )
                        self._geo_blocked = True
                else:
                    logger.warning("get_ticker ccxt error (%s): %s. Trying public Binance Vision endpoint...", symbol, exc)

        real_p = self._fetch_fallback_ticker(symbol)
        if real_p is not None:
            return real_p

        logger.error("Failed to fetch ticker for %s from all Binance endpoints.", symbol)
        return None

    # ------------------------------------------------------------------
    # Account balance
    # ------------------------------------------------------------------
    def get_account_balance(self) -> Optional[float]:
        """
        Return the free USDT balance, or None if unauthenticated / on error.
        """
        if self._exchange is None:
            return None
        if not self._authenticated:
            logger.warning("get_account_balance called without API credentials")
            return None
        try:
            balance = self._exchange.fetch_balance()
            usdt = balance.get("USDT") or balance.get("free", {}).get("USDT")
            if usdt is None:
                # Futures balance structure
                usdt = (
                    balance.get("info", {})
                    .get("assets", [{}])
                )
                for asset in usdt if isinstance(usdt, list) else []:
                    if asset.get("asset") == "USDT":
                        return float(asset.get("availableBalance", 0))
                return None
            if isinstance(usdt, dict):
                return float(usdt.get("free", 0))
            return float(usdt)
        except Exception as exc:
            logger.error("get_account_balance error: %s", exc)
            return None
