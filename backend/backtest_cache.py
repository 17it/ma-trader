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
    if last_ts + step_ms * 2 < effective_until:
        missing.append((last_ts + step_ms, effective_until))

    return missing


def _fetch_interval_from_network(
    exchange,
    clean_symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int
) -> List[list]:
    """Fetch candles for [start_ms, end_ms) via CCXT or Binance Vision API with pagination."""
    bars_fetched = []
    curr = start_ms
    limit = 1000

    # 1. Try CCXT first if available
    ccxt_failed = False
    if exchange is not None:
        while curr < end_ms:
            try:
                raw = exchange.fetch_ohlcv(clean_symbol, timeframe, since=curr, limit=limit)
                if not raw:
                    break
                bars_fetched.extend(raw)
                last_ts = raw[-1][0]
                if last_ts >= end_ms or len(raw) < limit:
                    break
                curr = last_ts + 1
            except Exception as exc:
                logger.warning(f"[Cache] CCXT fetch error for {clean_symbol}: {exc}")
                ccxt_failed = True
                break

    # 2. If CCXT failed or returned insufficient bars, try official public Binance Vision API
    if ccxt_failed or not bars_fetched:
        import httpx
        curr = start_ms
        while curr < end_ms:
            url = f"https://data-api.binance.vision/api/v3/klines?symbol={clean_symbol}&interval={timeframe}&startTime={curr}&limit={limit}"
            try:
                r = httpx.get(url, timeout=5.0)
                if r.status_code == 200:
                    raw = r.json()
                    if not raw or not isinstance(raw, list):
                        break
                    for row in raw:
                        ts = int(row[0])
                        if ts < end_ms:
                            bars_fetched.append([
                                ts,
                                float(row[1]),
                                float(row[2]),
                                float(row[3]),
                                float(row[4]),
                                float(row[5]),
                            ])
                    last_ts = int(raw[-1][0])
                    if len(raw) < limit or last_ts >= end_ms:
                        break
                    curr = last_ts + 1
                else:
                    logger.warning(f"[Cache] Binance Vision returned {r.status_code} for {clean_symbol}")
                    break
            except Exception as e:
                logger.warning(f"[Cache] Binance Vision request error: {e}")
                break

    return bars_fetched


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
    4. Returns complete dataset sorted chronologically.
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
    elapsed = time.time() - t0
    logger.info(
        f"[Backtest Cache] ✅ Finished loading {len(all_bars):,} bars "
        f"(newly fetched: {new_bars_total:,}) in {elapsed:.2f}s."
    )
    return all_bars
