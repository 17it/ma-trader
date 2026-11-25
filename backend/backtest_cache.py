"""
Backtest K-line Local Cache Module.
Provides incremental fetching and SQLite persistent storage for CLOSED Futures OHLCV bars.

Safety guarantees:
  - Never caches still-forming (unclosed) candles (open_time + step_ms > now_ms).
  - Prefers Binance USD-M Futures klines (fapi.binance.com) to match the Futures strategy,
    falling back to Binance Vision only when fapi is unavailable/geo-blocked.
  - Never persists potentially incomplete resampled HTF bars into SQLite cache.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx
from sqlalchemy import text

from database import SessionLocal, ensure_tables

logger = logging.getLogger(__name__)


def _timeframe_to_ms(tf: str) -> int:
    mapping = {
        "1m": 60 * 1000,
        "3m": 3 * 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "2h": 2 * 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "6h": 6 * 60 * 60 * 1000,
        "12h": 12 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    return mapping.get(tf, 60 * 1000)


def get_cached_klines(symbol: str, timeframe: str, since_ms: int, until_ms: int) -> List[list]:
    """Retrieve cached bars [ts, open, high, low, close, volume] from local SQLite."""
    db = SessionLocal()
    try:
        stmt = text("""
            SELECT open_time, open, high, low, close, volume
            FROM cached_klines
            WHERE symbol = :symbol AND timeframe = :timeframe
              AND open_time >= :since_ms AND open_time < :until_ms
            ORDER BY open_time ASC
        """)
        rows = db.execute(stmt, {
            "symbol": symbol,
            "timeframe": timeframe,
            "since_ms": since_ms,
            "until_ms": until_ms,
        }).fetchall()
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows]
    except Exception as exc:
        logger.warning("[Cache] Error querying cached klines: %s", exc)
        return []
    finally:
        db.close()


def save_klines_to_cache(symbol: str, timeframe: str, bars: List[list]) -> int:
    """Bulk save CLOSED bars into SQLite using INSERT OR IGNORE."""
    if not bars:
        return 0
    step_ms = _timeframe_to_ms(timeframe)
    now_ms = int(time.time() * 1000)
    # Never cache unclosed forming candles — INSERT OR IGNORE would freeze stale partial values forever
    closed_bars = [b for b in bars if (int(b[0]) + step_ms) <= now_ms]
    if not closed_bars:
        return 0

    db = SessionLocal()
    try:
        stmt = text("""
            INSERT OR IGNORE INTO cached_klines
            (symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES (:symbol, :timeframe, :open_time, :open, :high, :low, :close, :volume)
        """)
        params = [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": int(b[0]),
                "open": float(b[1]),
                "high": float(b[2]),
                "low": float(b[3]),
                "close": float(b[4]),
                "volume": float(b[5]),
            }
            for b in closed_bars
        ]
        chunk_size = 2000
        for i in range(0, len(params), chunk_size):
            db.execute(stmt, params[i:i + chunk_size])
        db.commit()
        return len(closed_bars)
    except Exception as exc:
        db.rollback()
        logger.warning("[Cache] Error saving klines to cache: %s", exc)
        return 0
    finally:
        db.close()


def find_missing_intervals(
    cached_bars: List[list],
    since_ms: int,
    until_ms: int,
    step_ms: int,
) -> List[Tuple[int, int]]:
    """
    Compute [start_ms, end_ms) intervals missing from [since_ms, until_ms).
    Caps until_ms at the last fully closed candle boundary.
    """
    now_ms = int(time.time() * 1000)
    last_closed_boundary = (now_ms // step_ms) * step_ms
    effective_until = min(until_ms, last_closed_boundary)

    if since_ms >= effective_until:
        return []

    if not cached_bars:
        return [(since_ms, effective_until)]

    missing = []
    first_ts = cached_bars[0][0]
    if first_ts > since_ms + step_ms:
        missing.append((since_ms, first_ts))

    for i in range(len(cached_bars) - 1):
        curr_ts = cached_bars[i][0]
        next_ts = cached_bars[i + 1][0]
        if next_ts - curr_ts > step_ms * 2:
            missing.append((curr_ts + step_ms, next_ts))

    last_ts = cached_bars[-1][0]
    if last_ts + max(step_ms * 4, 3600_000) < effective_until:
        missing.append((last_ts + step_ms, effective_until))

    return missing


def _get_active_proxy() -> Optional[str]:
    """Auto-detect proxy from user settings in DB or environment."""
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

    if not proxy:
        return None
    if proxy.startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]
    return proxy


def _fetch_interval_from_network(
    exchange,
    clean_symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> List[list]:
    """
    Fetch historical candles for [start_ms, end_ms).
    Tries Binance USD-M Futures first (matches Futures trading strategy),
    falling back to Binance Vision public data API if Futures API is geo-blocked (451).
    """
    bars_fetched = []
    curr = start_ms
    limit = 1000
    proxy_url = _get_active_proxy()
    step_ms = _timeframe_to_ms(timeframe)

    # Prefer Futures endpoint first, switch to Vision if geo-blocked
    base_urls = [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://data-api.binance.vision/api/v3/klines",
    ]
    active_url_idx = 0

    client_kwargs = {"timeout": 15.0}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    with httpx.Client(**client_kwargs) as client:
        while curr < end_ms:
            success = False
            for attempt in range(4):
                base_url = base_urls[active_url_idx]
                url = f"{base_url}?symbol={clean_symbol}&interval={timeframe}&startTime={curr}&limit={limit}"
                try:
                    r = client.get(url)
                    if r.status_code == 200:
                        raw = r.json()
                        if not raw or not isinstance(raw, list):
                            curr = end_ms
                            success = True
                            break
                        now_ms = int(time.time() * 1000)
                        batch = []
                        for row in raw:
                            ts = int(row[0])
                            if ts < end_ms and (ts + step_ms) <= now_ms:
                                batch.append([
                                    ts,
                                    float(row[1]),
                                    float(row[2]),
                                    float(row[3]),
                                    float(row[4]),
                                    float(row[5]),
                                ])
                        if batch:
                            bars_fetched.extend(batch)
                            save_klines_to_cache(clean_symbol, timeframe, batch)

                        last_ts = int(raw[-1][0])
                        if len(raw) < limit or last_ts >= end_ms:
                            curr = end_ms
                        else:
                            curr = last_ts + 1
                        success = True
                        break
                    elif r.status_code == 451 and active_url_idx + 1 < len(base_urls):
                        logger.warning("[Cache] %s returned HTTP 451; switching to Binance Vision fallback.", base_url)
                        active_url_idx += 1
                    elif r.status_code == 429:
                        time.sleep(1.0 * (attempt + 1))
                    else:
                        logger.warning("[Cache] %s returned %s for %s (attempt %d)", base_url, r.status_code, clean_symbol, attempt + 1)
                        if active_url_idx + 1 < len(base_urls) and attempt >= 1:
                            active_url_idx += 1
                        time.sleep(0.5)
                except Exception as e:
                    logger.warning("[Cache] Fetch error on %s (%s, attempt %d): %s", clean_symbol, base_url, attempt + 1, e)
                    if active_url_idx + 1 < len(base_urls) and attempt >= 1:
                        active_url_idx += 1
                    time.sleep(1.0 * (attempt + 1))

            if not success:
                logger.error("[Cache] Failed to fetch interval starting at %s after 4 attempts.", curr)
                break

    return bars_fetched


def _resample_from_1m_cache(symbol: str, target_timeframe: str, since_ms: int, until_ms: int) -> List[list]:
    """
    Synthesize target_timeframe bars in memory from local 1m cache.
    Does NOT persist synthetic bars into cached_klines to avoid permanently caching
    partially-populated HTF bars when 1m data has gaps.
    """
    import pandas as pd
    bars_1m = get_cached_klines(symbol, "1m", since_ms, until_ms)
    if len(bars_1m) < 10:
        return []
    try:
        df = pd.DataFrame(bars_1m, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms")
        rule = target_timeframe.replace("m", "min")
        resampled = df.set_index("dt").resample(rule).agg({
            "ts": "first",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna().reset_index(drop=True)
        return [
            [int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])]
            for _, r in resampled.iterrows()
        ]
    except Exception as exc:
        logger.warning("[Cache] Resample from 1m failed: %s", exc)
        return []


def load_ohlcv_with_cache(
    exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
) -> List[list]:
    """Load CLOSED OHLCV bars using incremental SQLite caching."""
    ensure_tables()

    clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
    step_ms = _timeframe_to_ms(timeframe)

    t0 = time.time()
    cached_bars = get_cached_klines(clean_symbol, timeframe, since_ms, until_ms)
    missing = find_missing_intervals(cached_bars, since_ms, until_ms, step_ms)

    if not missing:
        elapsed = time.time() - t0
        logger.info(
            "[Backtest Cache] 100%% Cache HIT for %s %s (%s bars in %.3fs)",
            clean_symbol, timeframe, f"{len(cached_bars):,}", elapsed,
        )
        return cached_bars

    logger.info(
        "[Backtest Cache] Found %s cached bars. Fetching %d missing interval(s) for %s %s...",
        f"{len(cached_bars):,}", len(missing), clean_symbol, timeframe,
    )

    new_bars_total = 0
    for start_ms, end_ms in missing:
        start_str = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        end_str = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        logger.info("[Backtest Cache] Fetching missing interval: %s ~ %s", start_str, end_str)

        fetched = _fetch_interval_from_network(exchange, clean_symbol, timeframe, start_ms, end_ms)
        if fetched:
            new_bars_total += len(fetched)

    all_bars = get_cached_klines(clean_symbol, timeframe, since_ms, until_ms)

    now_ms = int(time.time() * 1000)
    effective_until = min(until_ms, now_ms)
    expected_bars = max(10, (effective_until - since_ms) // step_ms)
    if len(all_bars) < expected_bars * 0.7 and timeframe != "1m":
        logger.info(
            "[Backtest Cache] Incomplete %s data (%d/%d bars). Synthesizing in memory from 1m cache...",
            timeframe, len(all_bars), expected_bars,
        )
        resampled = _resample_from_1m_cache(clean_symbol, timeframe, since_ms, until_ms)
        if len(resampled) > len(all_bars):
            all_bars = resampled

    elapsed = time.time() - t0
    logger.info(
        "[Backtest Cache] Finished loading %s bars (newly fetched: %s) in %.2fs.",
        f"{len(all_bars):,}", f"{new_bars_total:,}", elapsed,
    )
    return all_bars
