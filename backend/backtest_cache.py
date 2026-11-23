"""
Backtest K-line Local Cache Module.
Provides incremental fetching and SQLite persistent storage for OHLCV bars.
"""
import time
import logging
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from sqlalchemy import text
from database import SessionLocal, create_tables

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
            "until_ms": until_ms
        }).fetchall()
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows]
    except Exception as exc:
        logger.warning(f"[Cache] Error querying cached klines: {exc}")
        return []
    finally:
        db.close()


def save_klines_to_cache(symbol: str, timeframe: str, bars: List[list]) -> int:
    """Bulk save bars [ts, open, high, low, close, volume] into SQLite using INSERT OR IGNORE."""
    if not bars:
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
            for b in bars
        ]
        # Chunk inserts to 2000 items per batch to avoid SQLite parameter limits
        chunk_size = 2000
        for i in range(0, len(params), chunk_size):
            db.execute(stmt, params[i:i + chunk_size])
        db.commit()
        return len(bars)
    except Exception as exc:
        db.rollback()
        logger.warning(f"[Cache] Error saving klines to cache: {exc}")
        return 0
    finally:
        db.close()


def find_missing_intervals(
    cached_bars: List[list],
    since_ms: int,
    until_ms: int,
    step_ms: int
) -> List[Tuple[int, int]]:
    """
    Given already cached bars, compute list of [start_ms, end_ms) intervals
    that are missing from [since_ms, until_ms).
    Caps until_ms at the current wall-clock time since future candles do not exist.
    """
    now_ms = int(time.time() * 1000)
    effective_until = min(until_ms, now_ms)

    if since_ms >= effective_until:
        return []

    if not cached_bars:
        return [(since_ms, effective_until)]

    missing = []
    first_ts = cached_bars[0][0]
    # Check if beginning is missing
    if first_ts > since_ms + step_ms:
        missing.append((since_ms, first_ts))

    # Check internal gaps (gap larger than 2 candles)
    for i in range(len(cached_bars) - 1):
        curr_ts = cached_bars[i][0]
        next_ts = cached_bars[i + 1][0]
        if next_ts - curr_ts > step_ms * 2:
            missing.append((curr_ts + step_ms, next_ts))

    # Check if end is missing (do not request future candles beyond now_ms)
    last_ts = cached_bars[-1][0]
    # If the gap at the end is smaller than 2 hours or within recent time, don't stall backtest
    if last_ts + max(step_ms * 4, 3600_000) < effective_until:
        missing.append((last_ts + step_ms, effective_until))

    return missing


def _get_active_proxy() -> Optional[str]:
    """Auto-detect proxy from user settings in DB or environment. No hardcoded addresses."""
    proxy = ""
    try:
        from database import SessionLocal, SettingModel
        db = SessionLocal()
        setting = db.query(SettingModel).filter(SettingModel.key == "http_proxy").first()
        if setting and setting.value and setting.value.strip():
            proxy = setting.value.strip()
        db.close()
    except Exception:
        pass

    if not proxy:
        import os
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
            or ""
        ).strip()

    return proxy if proxy else None


def _fetch_interval_from_network(
    exchange,
    clean_symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int
) -> List[list]:
    """
    Fetch candles for [start_ms, end_ms) with retry, proxy support,
    and progressive saving to SQLite for robust resumable fetching.
    """
    import httpx
    bars_fetched = []
    curr = start_ms
    limit = 1000
    proxy_url = _get_active_proxy()

    while curr < end_ms:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&startTime={curr}&limit={limit}"
        success = False
        for attempt in range(4):
            try:
                client_kwargs = {"timeout": 15.0}
                if proxy_url:
                    client_kwargs["proxy"] = proxy_url
                with httpx.Client(**client_kwargs) as client:
                    r = client.get(url)
                if r.status_code == 200:
                    raw = r.json()
                    if not raw or not isinstance(raw, list):
                        success = True
                        break
                    batch = []
                    for row in raw:
                        ts = int(row[0])
                        if ts < end_ms:
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
                        # Progressive save to avoid losing data on disconnect
                        save_klines_to_cache(clean_symbol, timeframe, batch)

                    last_ts = int(raw[-1][0])
                    if len(raw) < limit or last_ts >= end_ms:
                        curr = end_ms
                    else:
                        curr = last_ts + 1
                    success = True
                    break
                elif r.status_code == 429:
                    # Rate limit, wait a bit
                    time.sleep(1.0 * (attempt + 1))
                else:
                    logger.warning(f"[Cache] Binance Vision returned {r.status_code} for {clean_symbol} (attempt {attempt+1})")
                    time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[Cache] Binance Vision error on {clean_symbol} (attempt {attempt+1}): {e}")
                time.sleep(1.0 * (attempt + 1))

        if not success:
            logger.error(f"[Cache] Failed to fetch interval starting at {curr} after 4 attempts. Halting pagination.")
            break

    return bars_fetched


def _resample_from_1m_cache(symbol: str, target_timeframe: str, since_ms: int, until_ms: int) -> List[list]:
    """Attempt to synthesize target_timeframe bars from existing 1m local cached bars."""
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
            "volume": "sum"
        }).dropna().reset_index(drop=True)
        res_list = [
            [int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])]
            for _, r in resampled.iterrows()
        ]
        if res_list:
            save_klines_to_cache(symbol, target_timeframe, res_list)
        return res_list
    except Exception as exc:
        logger.warning(f"[Cache] Resample from 1m failed: {exc}")
        return []


def load_ohlcv_with_cache(
    exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int
) -> List[list]:
    """
    Main entry point: loads OHLCV bars using incremental SQLite caching.
    1. Checks local SQLite database for existing cached bars.
    2. Identifies missing intervals.
    3. Fetches ONLY missing intervals from Binance and saves them to SQLite.
    4. If network fails and timeframe != '1m', falls back to resampling from 1m cache.
    5. Returns complete dataset sorted chronologically.
    """
    create_tables()  # Ensure cached_klines table exists

    clean_symbol = symbol.replace("/", "").replace(":USDT", "").upper()
    step_ms = _timeframe_to_ms(timeframe)

    t0 = time.time()
    # 1. Check local cache
    cached_bars = get_cached_klines(clean_symbol, timeframe, since_ms, until_ms)

    # 2. Find missing intervals
    missing = find_missing_intervals(cached_bars, since_ms, until_ms, step_ms)

    if not missing:
        elapsed = time.time() - t0
        logger.info(
            f"[Backtest Cache] ⚡ 100% Cache HIT for {clean_symbol} {timeframe} "
            f"({len(cached_bars):,} bars loaded from SQLite in {elapsed:.3f}s)"
        )
        return cached_bars

    logger.info(
        f"[Backtest Cache] Found {len(cached_bars):,} cached bars. "
        f"Fetching {len(missing)} missing interval(s) for {clean_symbol} {timeframe}..."
    )

    # 3. Fetch only the missing ranges
    new_bars_total = 0
    for start_ms, end_ms in missing:
        start_str = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        end_str = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        logger.info(f"[Backtest Cache] 🌐 Fetching missing interval: {start_str} ~ {end_str}")

        fetched = _fetch_interval_from_network(exchange, clean_symbol, timeframe, start_ms, end_ms)
        if fetched:
            saved = save_klines_to_cache(clean_symbol, timeframe, fetched)
            new_bars_total += saved
            logger.info(f"[Backtest Cache] 💾 Cached {saved:,} new bars into SQLite.")

    # 4. Re-query complete dataset from SQLite
    all_bars = get_cached_klines(clean_symbol, timeframe, since_ms, until_ms)

    # 5. Fallback: If still incomplete and target timeframe != '1m', synthesize from 1m cache
    now_ms = int(time.time() * 1000)
    effective_until = min(until_ms, now_ms)
    expected_bars = max(10, (effective_until - since_ms) // step_ms)
    if len(all_bars) < expected_bars * 0.7 and timeframe != "1m":
        logger.info(f"[Backtest Cache] Incomplete {timeframe} data ({len(all_bars)}/{expected_bars} bars). Synthesizing from local 1m cache...")
        resampled = _resample_from_1m_cache(clean_symbol, timeframe, since_ms, until_ms)
        if len(resampled) > len(all_bars):
            all_bars = resampled
            logger.info(f"[Backtest Cache] ✅ Synthesized {len(all_bars):,} {timeframe} bars from 1m cache.")

    elapsed = time.time() - t0
    logger.info(
        f"[Backtest Cache] ✅ Finished loading {len(all_bars):,} bars "
        f"(newly fetched: {new_bars_total:,}) in {elapsed:.2f}s."
    )
    return all_bars
