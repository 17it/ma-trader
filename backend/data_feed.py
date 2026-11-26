"""
Data feed module — Binance OHLCV and account data via ccxt / httpx.
Supports both Futures and Spot, and enforces strict Futures-only market data
when allow_spot_fallback=False (live trading mode).
"""

import logging
import os
from typing import Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_httpx_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith("socks5h://"):
        return "socks5://" + p[len("socks5h://"):]
    return p or None


class DataFeed:
    """
    Thin wrapper around ccxt for Binance market data and account info.

    - Provide api_key / secret for authenticated (trading) mode.
    - use_futures=True (default) connects to Binance USD-M Futures.
    - allow_spot_fallback=False prevents silent fallback to Spot prices during live Futures trading.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        use_futures: bool = True,
        proxy: Optional[str] = None,
        ws_streamer: Optional[object] = None,
        allow_spot_fallback: bool = True,
    ):
        self._ws_streamer = ws_streamer
        self._use_futures = use_futures
        self._allow_spot_fallback = allow_spot_fallback
        self._geo_blocked = False
        self._api_key = api_key or ""
        self._secret = secret or ""
        self._authenticated = bool(api_key and secret)
        self._proxy = (proxy or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
        self._exchange = self._create_exchange()

    def _create_exchange(self):
        try:
            import ccxt
            options: dict = {}
            if self._use_futures:
                options["defaultType"] = "future"

            params = {
                "apiKey": self._api_key,
                "secret": self._secret,
                "options": options,
                "enableRateLimit": True,
                "timeout": 10000,
            }
            if self._proxy:
                params["proxies"] = {"http": self._proxy, "https": self._proxy}

            ex = ccxt.binance(params)
            logger.info(
                "DataFeed initialised (futures=%s, auth=%s, proxy=%s, spot_fallback=%s)",
                self._use_futures,
                self._authenticated,
                bool(self._proxy),
                self._allow_spot_fallback,
            )
            return ex
        except Exception as exc:
            logger.error("DataFeed init failed: %s", exc)
            return None

    def update_proxy(self, proxy: Optional[str]) -> None:
        """Hot-reload proxy setting without restarting the bot."""
        new_proxy = (proxy or "").strip() or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
        if new_proxy != self._proxy:
            self._proxy = new_proxy
            self._geo_blocked = False
            self._exchange = self._create_exchange()

    def update_credentials(self, api_key: Optional[str], secret: Optional[str], proxy: Optional[str] = None) -> None:
        """Hot-reload Binance API credentials and proxy on a running live DataFeed."""
        new_key = (api_key or "").strip()
        new_secret = (secret or "").strip()
        new_proxy = (
            (proxy or "").strip() if proxy is not None else self._proxy
        ) or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
        if new_key != self._api_key or new_secret != self._secret or new_proxy != self._proxy:
            self._api_key = new_key
            self._secret = new_secret
            self._authenticated = bool(new_key and new_secret)
            self._proxy = new_proxy
            self._geo_blocked = False
            self._exchange = self._create_exchange()

    def set_ws_streamer(self, ws_streamer):
        """Attach or update the WebSocket real-time price streamer."""
        self._ws_streamer = ws_streamer

    # ------------------------------------------------------------------
    # REST Fallbacks (respects allow_spot_fallback)
    # ------------------------------------------------------------------
    def _build_kline_urls(self, clean_symbol: str, timeframe: str, limit: int) -> list[str]:
        urls = []
        if self._use_futures and not self._geo_blocked:
            urls.append(f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}")
        if self._allow_spot_fallback or not self._use_futures:
            urls.append(f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}")
            if not self._geo_blocked:
                urls.append(f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&limit={limit}")
        return urls

    def _build_ticker_urls(self, clean_symbol: str) -> list[str]:
        urls = []
        if self._use_futures and not self._geo_blocked:
            urls.append(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={clean_symbol}")
        if self._allow_spot_fallback or not self._use_futures:
            urls.append(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={clean_symbol}")
            if not self._geo_blocked:
                urls.append(f"https://api.binance.com/api/v3/ticker/price?symbol={clean_symbol}")
        return urls

    def _proxy_candidates(self) -> list[Optional[str]]:
        p = _normalize_httpx_proxy(self._proxy)
        return [p, None] if p else [None]

    def _fetch_fallback_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> Optional[pd.DataFrame]:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        urls = self._build_kline_urls(clean_symbol, timeframe, limit)
        for url in urls:
            for proxy in self._proxy_candidates():
                try:
                    kwargs = {"timeout": 6.0}
                    if proxy:
                        kwargs["proxy"] = proxy
                    with httpx.Client(**kwargs) as client:
                        resp = client.get(url)
                    if resp.status_code == 200:
                        raw = resp.json()
                        if raw and isinstance(raw, list):
                            parsed = [
                                [row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
                                for row in raw
                            ]
                            df = pd.DataFrame(parsed, columns=["timestamp", "open", "high", "low", "close", "volume"])
                            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                            return df
                except Exception:
                    pass
        return None

    def _fetch_fallback_ticker(self, symbol: str) -> Optional[float]:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
        urls = self._build_ticker_urls(clean_symbol)
        for url in urls:
            for proxy in self._proxy_candidates():
                try:
                    kwargs = {"timeout": 4.0}
                    if proxy:
                        kwargs["proxy"] = proxy
                    with httpx.Client(**kwargs) as client:
                        resp = client.get(url)
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
        if self._exchange is not None and not self._geo_blocked:
            try:
                raw = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                if raw:
                    df = pd.DataFrame(
                        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                    for col in ("open", "high", "low", "close", "volume"):
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df = df.dropna(subset=["close"]).reset_index(drop=True)
                    return df
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "restricted location" in err_str.lower():
                    if not self._geo_blocked:
                        logger.warning(
                            "Binance API returned HTTP 451 (geo-restricted). "
                            "Spot fallback enabled=%s.",
                            self._allow_spot_fallback,
                        )
                        self._geo_blocked = True
                else:
                    logger.warning("get_ohlcv ccxt error (%s): %s", symbol, exc)

        real_df = self._fetch_fallback_ohlcv(symbol, timeframe, limit)
        if real_df is not None:
            return real_df

        logger.error(
            "Failed to fetch OHLCV for %s (futures=%s, spot_fallback=%s).",
            symbol, self._use_futures, self._allow_spot_fallback,
        )
        return None

    # ------------------------------------------------------------------
    # Ticker
    # ------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> Optional[float]:
        # 1. Zero-HTTP WebSocket cache (require Futures stream when spot fallback is disabled)
        if self._ws_streamer is not None:
            require_futures = self._use_futures and not self._allow_spot_fallback
            cached = self._ws_streamer.get_cached_price(symbol, require_futures=require_futures)
            if cached is not None and cached > 0:
                return cached

        # 2. REST fallback
        if self._exchange is not None and not self._geo_blocked:
            try:
                ticker = self._exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or ticker.get("close") or 0)
                if price > 0:
                    return price
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "restricted location" in err_str.lower():
                    if not self._geo_blocked:
                        logger.warning("Binance API returned HTTP 451 on fetch_ticker.")
                        self._geo_blocked = True
                else:
                    logger.warning("get_ticker ccxt error (%s): %s", symbol, exc)

        real_p = self._fetch_fallback_ticker(symbol)
        if real_p is not None:
            return real_p

        logger.error("Failed to fetch ticker for %s.", symbol)
        return None

    # ------------------------------------------------------------------
    # Account balance
    # ------------------------------------------------------------------
    def get_account_balance(self) -> Optional[float]:
        if self._exchange is None:
            return None
        if not self._authenticated:
            logger.warning("get_account_balance called without API credentials")
            return None
        try:
            balance = self._exchange.fetch_balance()
            usdt = balance.get("USDT") or balance.get("free", {}).get("USDT")
            if usdt is None:
                assets = balance.get("info", {}).get("assets", [])
                for asset in assets if isinstance(assets, list) else []:
                    if asset.get("asset") == "USDT":
                        return float(asset.get("availableBalance", 0))
                return None
            if isinstance(usdt, dict):
                return float(usdt.get("free", 0))
            return float(usdt)
        except Exception as exc:
            logger.error("get_account_balance error: %s", exc)
            return None
