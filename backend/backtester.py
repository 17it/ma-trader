"""
MA Trader - Backtesting Engine
Simulates the exact MAStrategy logic over historical Futures OHLCV data with:
  - Zero look-ahead bias (HTF lookup uses strictly CLOSED HTF candles: htf_ts + htf_ms <= ltf_close_ts)
  - Shared MAStrategy entry & exit rules (3-bar MA slope, false-breakout filter, bias guard, MA60 cross, MA7 trailing)
  - Realistic trading fees (default 0.04% per side) and slippage (default 0.01% per side)
  - Leverage-capped position sizing aligned with TradingBot
  - Daily-return annualized Sharpe ratio (365 trading days/year for crypto)
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

try:
    from backend.strategy import ATR_PERIOD, BREAKEVEN_BUFFER, MAStrategy, TRAILING_ACTIVATE_R
except ImportError:
    from strategy import ATR_PERIOD, BREAKEVEN_BUFFER, MAStrategy, TRAILING_ACTIVATE_R

logger = logging.getLogger(__name__)


def _compute_mas(closes: pd.Series):
    """Return (ma7, ma25, ma60) Series aligned with closes."""
    ma7 = closes.rolling(7).mean()
    ma25 = closes.rolling(25).mean()
    ma60 = closes.rolling(60).mean()
    return ma7, ma25, ma60


def _fetch_ohlcv_all(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list:
    """
    Fetch all OHLCV bars in [since_ms, until_ms) using incremental SQLite cache.
    Raises an explicit error if real market data cannot be obtained.
    """
    from backtest_cache import load_ohlcv_with_cache
    try:
        result = load_ohlcv_with_cache(exchange, symbol, timeframe, since_ms, until_ms)
    except Exception as exc:
        logger.error("[Backtest] Failed to load OHLCV data for %s %s: %s", symbol, timeframe, exc)
        raise RuntimeError(f"获取真实K线数据失败: {symbol} {timeframe} ({exc})，请检查网络或代理设置。")

    if not result or len(result) < 70:
        bars_count = len(result) if result else 0
        raise ValueError(
            f"未能获取到足够的真实K线数据: {symbol} {timeframe} "
            f"(当前仅获取到 {bars_count} 根，策略计算 MA60 均线至少需要 70 根真实数据)。"
            f"请检查网络/代理连接，或调整回测日期区间。"
        )
    return result


def _parse_dt(val: str, is_end: bool = False) -> datetime:
    """Parse YYYY-MM-DD or ISO datetime string (with optional tz) into UTC datetime."""
    s = (val or "").strip()
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt + timedelta(days=1) if is_end else dt
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _timeframe_to_ms(tf: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    num = int(tf[:-1])
    unit = units.get(tf[-1], 60_000)
    return num * unit


def _compute_daily_sharpe(timestamps_ms: list[int], equity_history: list[float]) -> float:
    """Compute annualized Sharpe ratio from UTC daily equity snapshots (365 days/yr)."""
    if len(equity_history) < 2 or len(timestamps_ms) != len(equity_history):
        return 0.0
    daily_close_eq: dict[int, float] = {}
    for ts, eq in zip(timestamps_ms, equity_history):
        day_bucket = ts // 86_400_000
        daily_close_eq[day_bucket] = eq
    if len(daily_close_eq) < 2:
        return 0.0
    ordered_eq = np.array([daily_close_eq[d] for d in sorted(daily_close_eq)], dtype=float)
    prev = ordered_eq[:-1]
    valid = prev > 0
    if not np.any(valid):
        return 0.0
    rets = np.diff(ordered_eq)[valid] / prev[valid]
    if len(rets) < 2:
        return 0.0
    std_r = float(rets.std(ddof=1))
    if std_r <= 0:
        return 0.0
    return float(rets.mean() / std_r * math.sqrt(365.0))


class Backtester:
    """
    Simulates MAStrategy faithfully over historical candles without look-ahead bias.
    """

    def __init__(self, exchange=None):
        self.exchange = exchange

    def run(
        self,
        symbol: str,
        timeframe: str,
        htf_timeframe: str,
        start_date: str,
        end_date: str,
        confirm_candles: int = 2,
        rr_ratio: float = 1.5,
        initial_capital: float = 10_000.0,
        risk_per_trade: float = 0.01,
        leverage: int = 5,
        fee_rate: float = 0.0004,
        slippage_rate: float = 0.0001,
        df_ltf_override: Optional[pd.DataFrame] = None,
        df_htf_override: Optional[pd.DataFrame] = None,
    ) -> dict:
        start_dt = _parse_dt(start_date, is_end=False)
        end_dt = _parse_dt(end_date, is_end=True)
        since_ms = int(start_dt.timestamp() * 1000)
        until_ms = int(end_dt.timestamp() * 1000)
        if until_ms <= since_ms:
            raise ValueError("结束时间必须晚于开始时间")
        tf_ms = _timeframe_to_ms(timeframe)
        htf_ms = _timeframe_to_ms(htf_timeframe)

        if df_ltf_override is not None:
            df_ltf = df_ltf_override.copy().reset_index(drop=True)
        else:
            ltf_fetch_since = since_ms - 70 * tf_ms
            logger.info("[Backtest] Fetching %s %s from %s to %s", symbol, timeframe, start_date, end_date)
            raw_ltf = _fetch_ohlcv_all(self.exchange, symbol, timeframe, ltf_fetch_since, until_ms)
            df_ltf = pd.DataFrame(raw_ltf, columns=["ts", "open", "high", "low", "close", "volume"])

        if len(df_ltf) < 70:
            raise ValueError(f"Insufficient LTF data: {len(df_ltf)} bars")

        if df_htf_override is not None:
            df_htf = df_htf_override.copy().reset_index(drop=True)
        else:
            htf_fetch_since = since_ms - 70 * htf_ms
            logger.info("[Backtest] Fetching HTF %s %s", symbol, htf_timeframe)
            raw_htf = _fetch_ohlcv_all(self.exchange, symbol, htf_timeframe, htf_fetch_since, until_ms)
            df_htf = pd.DataFrame(raw_htf, columns=["ts", "open", "high", "low", "close", "volume"])

        if len(df_htf) < 70 and len(df_ltf) >= 70:
            df_tmp = df_ltf.copy()
            df_tmp["dt"] = pd.to_datetime(df_tmp["ts"], unit="ms")
            rule = htf_timeframe.replace("m", "min")
            df_htf = df_tmp.set_index("dt").resample(rule).agg({
                "ts": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
            }).dropna().reset_index(drop=True)

        # Compute MAs & ATR(14)
        df_ltf["ma7"], df_ltf["ma25"], df_ltf["ma60"] = _compute_mas(df_ltf["close"])
        prev_close = df_ltf["close"].shift(1)
        tr = pd.concat(
            [
                df_ltf["high"] - df_ltf["low"],
                (df_ltf["high"] - prev_close).abs(),
                (df_ltf["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df_ltf["atr14"] = tr.rolling(ATR_PERIOD).mean()
        df_htf["ma7"], df_htf["ma25"], df_htf["ma60"] = _compute_mas(df_htf["close"])

        # HTF array: [open_ts, close, ma7, ma25, ma60]
        htf_ts_arr = df_htf["ts"].to_numpy(dtype=np.int64)
        htf_close_arr = df_htf["close"].to_numpy(dtype=float)
        htf_ma7_arr = df_htf["ma7"].to_numpy(dtype=float)
        htf_ma25_arr = df_htf["ma25"].to_numpy(dtype=float)
        htf_ma60_arr = df_htf["ma60"].to_numpy(dtype=float)

        def get_closed_htf_direction(ltf_open_ts: int) -> Optional[str]:
            """
            Return HTF direction using ONLY the latest HTF candle that has FULLY CLOSED
            at or before the current LTF candle's close time (ltf_open_ts + tf_ms).
            Prevents look-ahead bias completely.
            """
            if len(htf_ts_arr) == 0:
                return None
            max_closed_htf_open_ts = (ltf_open_ts + tf_ms) - htf_ms
            idx = int(np.searchsorted(htf_ts_arr, max_closed_htf_open_ts, side="right")) - 1
            if idx < 59 or idx >= len(htf_ts_arr):
                return None
            return MAStrategy.check_htf_scalars(
                htf_close_arr[idx], htf_ma7_arr[idx], htf_ma25_arr[idx], htf_ma60_arr[idx]
            )

        # Extract LTF columns to numpy arrays for fast iteration
        ltf_ts = df_ltf["ts"].to_numpy(dtype=np.int64)
        ltf_open = df_ltf["open"].to_numpy(dtype=float)
        ltf_high = df_ltf["high"].to_numpy(dtype=float)
        ltf_low = df_ltf["low"].to_numpy(dtype=float)
        ltf_close = df_ltf["close"].to_numpy(dtype=float)
        ltf_ma7 = df_ltf["ma7"].to_numpy(dtype=float)
        ltf_ma25 = df_ltf["ma25"].to_numpy(dtype=float)
        ltf_ma60 = df_ltf["ma60"].to_numpy(dtype=float)
        ltf_atr14 = df_ltf["atr14"].to_numpy(dtype=float)

        strategy = MAStrategy()

        capital = float(initial_capital)
        total_fees_paid = 0.0
        equity_history: list[float] = []
        equity_ts: list[int] = []
        trades_result: list[dict] = []

        in_trade = False
        trade_direction: Optional[str] = None
        trade_entry: float = 0.0
        trade_sl: float = 0.0
        trade_tp: float = 0.0
        trade_risk_r: float = 0.0
        trade_qty: float = 0.0
        trade_entry_time: int = 0
        trade_entry_fee: float = 0.0
        trailing_active: bool = False

        def record_close(exit_raw_price: float, ts_ms: int, close_reason: str):
            nonlocal capital, total_fees_paid, in_trade, trailing_active
            if trade_direction == "long":
                exit_fill = exit_raw_price * (1.0 - slippage_rate)
                gross_pnl = (exit_fill - trade_entry) * trade_qty
            else:
                exit_fill = exit_raw_price * (1.0 + slippage_rate)
                gross_pnl = (trade_entry - exit_fill) * trade_qty

            exit_fee = exit_fill * trade_qty * fee_rate
            trade_fee = trade_entry_fee + exit_fee
            net_pnl = gross_pnl - trade_fee
            notional = trade_entry * trade_qty
            pnl_pct = (net_pnl / notional * 100.0) if notional > 0 else 0.0

            capital += net_pnl
            total_fees_paid += trade_fee
            trades_result.append({
                "entry_time": datetime.fromtimestamp(trade_entry_time / 1000, tz=timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
                "direction": trade_direction,
                "entry_price": round(trade_entry, 6),
                "exit_price": round(exit_fill, 6),
                "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "fee": round(trade_fee, 4),
                "close_reason": close_reason,
                "result": "win" if net_pnl > 0 else "loss",
            })
            in_trade = False
            trailing_active = False

        min_idx = 62  # need MA60 + 3-bar MA slope history
        sim_start_idx = min_idx
        while sim_start_idx < len(ltf_ts) and int(ltf_ts[sim_start_idx]) < since_ms:
            sim_start_idx += 1

        for i in range(sim_start_idx, len(ltf_ts)):
            ts = int(ltf_ts[i])
            o, h, l, c = ltf_open[i], ltf_high[i], ltf_low[i], ltf_close[i]
            ma7, ma25, ma60 = ltf_ma7[i], ltf_ma25[i], ltf_ma60[i]

            if math.isnan(ma7) or math.isnan(ma25) or math.isnan(ma60):
                equity_history.append(capital)
                equity_ts.append(ts)
                continue

            # ---- 1. Manage open trade (no 0.7R BE / no MA60 exit; 1.2R -> BE+0.1% + MA25 trailing) ----
            if in_trade:
                if trade_direction == "long":
                    if l <= trade_sl:
                        reason = "trailing_stop" if trailing_active else "stop_loss"
                        record_close(trade_sl, ts, reason)
                    elif not trailing_active:
                        if trade_risk_r > 0 and h >= trade_entry + TRAILING_ACTIVATE_R * trade_risk_r:
                            trade_sl = max(trade_sl, trade_entry * (1.0 + BREAKEVEN_BUFFER))
                            trailing_active = True
                    else:
                        exit_sig = MAStrategy.evaluate_exit_bar("long", c, ma25, True)
                        if exit_sig:
                            record_close(c, ts, exit_sig)
                        else:
                            trade_sl = max(trade_sl, ma25 * 0.9995)
                else:  # short
                    if h >= trade_sl:
                        reason = "trailing_stop" if trailing_active else "stop_loss"
                        record_close(trade_sl, ts, reason)
                    elif not trailing_active:
                        if trade_risk_r > 0 and l <= trade_entry - TRAILING_ACTIVATE_R * trade_risk_r:
                            trade_sl = min(trade_sl, trade_entry * (1.0 - BREAKEVEN_BUFFER))
                            trailing_active = True
                    else:
                        exit_sig = MAStrategy.evaluate_exit_bar("short", c, ma25, True)
                        if exit_sig:
                            record_close(c, ts, exit_sig)
                        else:
                            trade_sl = min(trade_sl, ma25 * 1.0005)

            # ---- 2. Always feed MAStrategy bar-close signal state machine ----
            htf_dir = get_closed_htf_direction(ts)
            sig = strategy.evaluate_bar(
                o=o,
                h=h,
                l=l,
                c=c,
                ma7_seq=(ltf_ma7[i - 2], ltf_ma7[i - 1], ma7),
                ma25_seq=(ltf_ma25[i - 2], ltf_ma25[i - 1], ma25),
                ma60=ma60,
                atr14=ltf_atr14[i],
                htf_dir=htf_dir,
                confirm_candles=confirm_candles,
            )

            if not in_trade and sig is not None and not sig.get("pending") and capital > 0:
                direction = sig["direction"]
                entry_fill = c * (1.0 + slippage_rate) if direction == "long" else c * (1.0 - slippage_rate)
                sl, tp, risk_r = MAStrategy.compute_sl_tp(
                    direction=direction,
                    entry_price=entry_fill,
                    ma60=ma60,
                    candle_low=l,
                    candle_high=h,
                    rr_ratio=rr_ratio,
                )
                if risk_r > 0:
                    risk_amount = capital * risk_per_trade
                    raw_qty = risk_amount / risk_r
                    # Cap notional exposure within available margin * leverage (aligned with TradingBot)
                    max_notional = capital * leverage * 0.95
                    if raw_qty * entry_fill > max_notional:
                        raw_qty = max_notional / entry_fill
                    qty = round(raw_qty, 6)
                    if qty > 0:
                        in_trade = True
                        trailing_active = False
                        trade_direction = direction
                        trade_entry = entry_fill
                        trade_sl = sl
                        trade_tp = tp
                        trade_risk_r = risk_r
                        trade_qty = qty
                        trade_entry_time = ts
                        trade_entry_fee = entry_fill * qty * fee_rate
                        strategy.reset()

            equity_history.append(capital)
            equity_ts.append(ts)

        # ---- Compute statistics ----
        total = len(trades_result)
        wins = sum(1 for t in trades_result if t["result"] == "win")
        losses = total - wins
        win_rate = wins / total * 100.0 if total > 0 else 0.0
        total_pnl = sum(t["pnl"] for t in trades_result)
        total_pnl_pct = (total_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0

        equity_arr = np.array(equity_history, dtype=float) if equity_history else np.array([initial_capital], dtype=float)
        peak = np.maximum.accumulate(equity_arr)
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown = np.where(peak > 0, (peak - equity_arr) / peak, 0.0)
        max_drawdown = float(drawdown.max()) * 100.0 if len(drawdown) else 0.0

        sharpe = _compute_daily_sharpe(equity_ts, equity_history)

        step = max(1, len(equity_history) // 500)
        equity_curve = []
        for idx in range(0, len(equity_history), step):
            ts_val = equity_ts[idx]
            dt_str = datetime.fromtimestamp(ts_val / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
            equity_curve.append({
                "time": dt_str,
                "date": dt_str,
                "t": dt_str,
                "equity": round(equity_history[idx], 2),
            })

        return {
            "total_trades": total,
            "win_trades": wins,
            "loss_trades": losses,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 4),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "total_fees": round(total_fees_paid, 4),
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 4),
            "trades": trades_result,
            "equity_curve": equity_curve,
        }
