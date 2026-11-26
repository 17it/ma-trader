"""
MA Strategy Module — MA7/MA25/MA60 trend alignment + ATR(14) adaptive bias +
adverse-wick entry filter + 1.2R break-even & MA25 trailing exit.

Single source of truth shared by both live/paper TradingBot and Backtester.
Each symbol gets its own MAStrategy instance so confirm_count is never shared.
"""

import logging
import math
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

ATR_PERIOD = 14
ATR_MA25_BIAS_MULT = 2.5
WICK_BODY_MULT = 1.5
TRAILING_ACTIVATE_R = 0.88
BREAKEVEN_BUFFER = 0.003  # +0.3% / -0.3% profit-lock buffer
DEFAULT_FEE_RATE = 0.0004
DEFAULT_SLIPPAGE_RATE = 0.0001


class MAStrategy:
    """Moving-average alignment strategy for a single trading symbol."""

    def __init__(self):
        self.confirm_count: int = 0
        self._last_direction: Optional[str] = None
        self._entry_time: Optional[datetime] = None

    def reset(self) -> None:
        self.confirm_count = 0
        self._last_direction = None

    # ------------------------------------------------------------------
    # Shared SL/TP computation (used by both TradingBot and Backtester)
    # ------------------------------------------------------------------
    @staticmethod
    def compute_sl_tp(
        direction: str,
        entry_price: float,
        ma60: float,
        candle_low: float,
        candle_high: float,
        rr_ratio: float = 1.5,
        min_sl_pct: float = 0.003,
    ) -> Tuple[float, float, float]:
        """
        Compute (stop_loss, take_profit, sl_distance) using MA60 or candle extreme,
        enforcing at least *min_sl_pct* (0.3%) distance from entry.
        """
        if direction == "long":
            sl_candidate = ma60 if (not math.isnan(ma60) and ma60 < entry_price) else candle_low
            stop_loss = min(sl_candidate, entry_price * (1.0 - min_sl_pct))
            sl_distance = entry_price - stop_loss
            take_profit = entry_price + sl_distance * rr_ratio
        else:
            sl_candidate = ma60 if (not math.isnan(ma60) and ma60 > entry_price) else candle_high
            stop_loss = max(sl_candidate, entry_price * (1.0 + min_sl_pct))
            sl_distance = stop_loss - entry_price
            take_profit = entry_price - sl_distance * rr_ratio
        return stop_loss, take_profit, max(0.0, sl_distance)

    @staticmethod
    def calc_position_size(
        capital: float,
        risk_pct: float,
        entry_price: float,
        sl_distance: float,
        leverage: int = 5,
        fee_rate: float = DEFAULT_FEE_RATE,
        slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    ) -> float:
        """
        Fee- and slippage-aware risk-based position sizing (shared by live & backtest).
        Includes estimated round-trip trading fees + slippage in per-unit risk so a
        stop-loss hit never exceeds the configured *risk_pct* of account capital even
        when the stop distance is tight.
        """
        if capital <= 0 or risk_pct <= 0 or entry_price <= 0 or sl_distance <= 0:
            return 0.0
        round_trip_friction = entry_price * (2.0 * max(0.0, fee_rate) + 2.0 * max(0.0, slippage_rate))
        effective_risk_per_unit = sl_distance + round_trip_friction
        raw_qty = (capital * risk_pct) / effective_risk_per_unit
        max_notional = capital * leverage * 0.95
        if raw_qty * entry_price > max_notional:
            raw_qty = max_notional / entry_price
        return round(raw_qty, 6)

    @staticmethod
    def compute_trailing_lock_sl(
        direction: str,
        entry_price: float,
        risk_r: float,
        current_stop_loss: float,
        activate_r: float = TRAILING_ACTIVATE_R,
        breakeven_buffer: float = BREAKEVEN_BUFFER,
    ) -> float:
        """
        Compute the locked stop-loss when floating profit reaches *activate_r* (default 0.88R).
        Targets entry ± *breakeven_buffer* (default ±0.3%), safely capped inside the trigger price
        so ultra-tight 0.30% stops never produce an SL beyond the live price (which Binance
        would reject as immediately triggering).
        """
        if direction == "long":
            trigger_price = entry_price + activate_r * risk_r
            target_lock = min(entry_price * (1.0 + breakeven_buffer), trigger_price * 0.9995)
            return max(current_stop_loss, target_lock)
        trigger_price = entry_price - activate_r * risk_r
        target_lock = max(entry_price * (1.0 - breakeven_buffer), trigger_price * 1.0005)
        return min(current_stop_loss, target_lock)

    # ------------------------------------------------------------------
    # Indicator calculation (MA7 / MA25 / MA60 + ATR14)
    # ------------------------------------------------------------------
    def calculate_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ma7, ma25, ma60, and atr14 columns to a copy of *df* and return it."""
        df = df.copy()
        df["ma7"] = df["close"].rolling(window=7, min_periods=1).mean()
        df["ma25"] = df["close"].rolling(window=25, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr14"] = tr.rolling(window=ATR_PERIOD, min_periods=1).mean()
        return df

    # ------------------------------------------------------------------
    # Scalar primitives (shared between live DataFrame path and fast backtest loop)
    # ------------------------------------------------------------------
    @staticmethod
    def check_alignment_scalars(
        close: float,
        ma7_seq: Tuple[float, float, float],
        ma25_seq: Tuple[float, float, float],
        ma60: float,
    ) -> Optional[str]:
        """
        Long  : close > ma7 > ma25 > ma60  AND  ma7/ma25 strictly rising for 3 bars.
        Short : close < ma7 < ma25 < ma60  AND  ma7/ma25 strictly falling for 3 bars.
        """
        m7_0, m7_1, m7_2 = ma7_seq
        m25_0, m25_1, m25_2 = ma25_seq
        if any(math.isnan(v) for v in (close, m7_0, m7_1, m7_2, m25_0, m25_1, m25_2, ma60)):
            return None

        ma7_rising = m7_0 < m7_1 < m7_2
        ma7_falling = m7_0 > m7_1 > m7_2
        ma25_rising = m25_0 < m25_1 < m25_2
        ma25_falling = m25_0 > m25_1 > m25_2

        if close > m7_2 > m25_2 > ma60 and ma7_rising and ma25_rising:
            return "long"
        if close < m7_2 < m25_2 < ma60 and ma7_falling and ma25_falling:
            return "short"
        return None

    @staticmethod
    def check_htf_scalars(close: float, ma7: float, ma25: float, ma60: float) -> Optional[str]:
        """
        Determine higher-timeframe trend from MA alignment alone (MA7 > MA25 > MA60 or vice-versa).
        Does NOT require close > ma7 so normal pullbacks inside an HTF uptrend never disable
        the HTF filter and permit counter-trend LTF entries.
        """
        if any(math.isnan(v) for v in (ma7, ma25, ma60)):
            return None
        if ma7 > ma25 > ma60:
            return "long"
        if ma7 < ma25 < ma60:
            return "short"
        return None

    @staticmethod
    def check_false_breakout_scalars(
        o: float,
        h: float,
        l: float,
        c: float,
        direction: Optional[str] = None,
        wick_mult: float = WICK_BODY_MULT,
    ) -> bool:
        """
        Adverse-wick-only false-breakout guard:
          - Long  : rejects long UPPER wick (h - max(o, c) > body * 1.5)
          - Short : rejects long LOWER wick (min(o, c) - l > body * 1.5)
          - Doji (body == 0 with non-zero range): rejected on entry bar
        Healthy pullback wicks in the trend direction (e.g. lower wick on a long)
        are NOT penalized.
        """
        body = abs(c - o)
        total_range = h - l
        if body == 0:
            return total_range > 0
        if direction == "long":
            adverse_wick = h - max(o, c)
        elif direction == "short":
            adverse_wick = min(o, c) - l
        else:
            adverse_wick = total_range - body
        return adverse_wick > body * wick_mult

    @staticmethod
    def check_overextended_scalars(
        close: float,
        ma25: float,
        atr14: float,
        atr_mult: float = ATR_MA25_BIAS_MULT,
    ) -> bool:
        """
        ATR(14)-adaptive anti-chasing guard (works across all timeframes):
        Return True when |close - ma25| > atr_mult * atr14 (default 2.5 * ATR14).
        """
        if math.isnan(close) or math.isnan(ma25) or math.isnan(atr14) or atr14 <= 0:
            return False
        return abs(close - ma25) > atr_mult * atr14

    @staticmethod
    def evaluate_exit_bar(
        direction: str,
        close: float,
        ma25: float,
        trailing_active: bool,
    ) -> Optional[str]:
        """
        Indicator-based bar-close exit rule shared by live and backtest:
          - Before trailing activates (< 1.2R): NO premature MA60 exit.
          - After trailing activates (>= 1.2R): exit when a candle closes beyond MA25.
        """
        if not trailing_active or math.isnan(close) or math.isnan(ma25):
            return None
        if direction == "long" and close < ma25:
            return "ma25_trailing"
        if direction == "short" and close > ma25:
            return "ma25_trailing"
        return None

    def evaluate_bar(
        self,
        o: float,
        h: float,
        l: float,
        c: float,
        ma7_seq: Tuple[float, float, float],
        ma25_seq: Tuple[float, float, float],
        ma60: float,
        atr14: float,
        htf_dir: Optional[str],
        confirm_candles: int = 2,
    ) -> Optional[dict]:
        """
        Single-bar entry signal state machine (identical for live and backtest):
          1. Three-line alignment + 3-bar MA7/MA25 slope
          2. HTF non-opposing direction filter
          3. ATR(14) adaptive bias guard: |close - MA25| <= 2.5 * ATR(14)
          4. Consecutive confirmation counter (NOT reset by adverse wicks)
          5. Entry-bar-only adverse-wick check once confirm_count >= confirm_candles
        """
        direction = self.check_alignment_scalars(c, ma7_seq, ma25_seq, ma60)
        if direction is None:
            self.reset()
            return None

        # HTF filter — skip if HTF has a clear opposing trend
        if htf_dir is not None and htf_dir != direction:
            self.reset()
            return None

        # ATR(14) adaptive overextension / anti-chasing guard
        if self.check_overextended_scalars(c, ma25_seq[2], atr14):
            self.reset()
            return None

        if direction != self._last_direction:
            self.confirm_count = 0
            self._last_direction = direction

        self.confirm_count = min(self.confirm_count + 1, confirm_candles)

        signal_info = {
            "direction": direction,
            "confirm": self.confirm_count,
            "max_confirm": confirm_candles,
        }
        if self.confirm_count < confirm_candles:
            return {**signal_info, "pending": True}

        # Entry-bar-only adverse-wick check: blocks entry on THIS bar without resetting confirm_count
        if self.check_false_breakout_scalars(o, h, l, c, direction=direction):
            return {**signal_info, "pending": True, "blocked_by_wick": True}

        return signal_info

    # ------------------------------------------------------------------
    # DataFrame wrappers (used by TradingBot)
    # ------------------------------------------------------------------
    def check_alignment(self, df: pd.DataFrame) -> Optional[str]:
        if len(df) < 3:
            return None
        last = df.iloc[-1]
        ma7_s = df["ma7"].iloc[-3:]
        ma25_s = df["ma25"].iloc[-3:]
        return self.check_alignment_scalars(
            float(last["close"]),
            (float(ma7_s.iloc[0]), float(ma7_s.iloc[1]), float(ma7_s.iloc[2])),
            (float(ma25_s.iloc[0]), float(ma25_s.iloc[1]), float(ma25_s.iloc[2])),
            float(last["ma60"]),
        )

    def check_false_breakout(self, df: pd.DataFrame, direction: Optional[str] = None) -> bool:
        if df.empty:
            return False
        last = df.iloc[-1]
        return self.check_false_breakout_scalars(
            float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"]),
            direction=direction,
        )

    def check_overextended(
        self,
        df: pd.DataFrame,
        atr_mult: float = ATR_MA25_BIAS_MULT,
    ) -> bool:
        if df.empty:
            return False
        if "atr14" not in df.columns or "ma25" not in df.columns:
            df = self.calculate_ma(df)
        last = df.iloc[-1]
        return self.check_overextended_scalars(
            float(last["close"]), float(last["ma25"]), float(last["atr14"]), atr_mult=atr_mult
        )

    def check_htf_direction(self, df_htf: pd.DataFrame) -> Optional[str]:
        if df_htf is None or len(df_htf) < 60:
            return None
        df_htf = self.calculate_ma(df_htf)
        last = df_htf.iloc[-1]
        return self.check_htf_scalars(
            float(last["close"]), float(last["ma7"]), float(last["ma25"]), float(last["ma60"])
        )

    def get_signal(
        self,
        df_1m: pd.DataFrame,
        df_htf: Optional[pd.DataFrame],
        confirm_candles: int = 2,
    ) -> Optional[dict]:
        if df_1m is None or len(df_1m) < 3:
            self.reset()
            return None
        df_1m = self.calculate_ma(df_1m)
        htf_dir = self.check_htf_direction(df_htf) if (df_htf is not None and len(df_htf) >= 60) else None
        last = df_1m.iloc[-1]
        ma7_s = df_1m["ma7"].iloc[-3:]
        ma25_s = df_1m["ma25"].iloc[-3:]
        return self.evaluate_bar(
            o=float(last["open"]),
            h=float(last["high"]),
            l=float(last["low"]),
            c=float(last["close"]),
            ma7_seq=(float(ma7_s.iloc[0]), float(ma7_s.iloc[1]), float(ma7_s.iloc[2])),
            ma25_seq=(float(ma25_s.iloc[0]), float(ma25_s.iloc[1]), float(ma25_s.iloc[2])),
            ma60=float(last["ma60"]),
            atr14=float(last["atr14"]),
            htf_dir=htf_dir,
            confirm_candles=confirm_candles,
        )

    def check_exit(self, df: pd.DataFrame, position: dict) -> Optional[str]:
        if df is None or df.empty:
            return None
        if not bool(position.get("trailing_active", False)):
            return None
        if "ma25" not in df.columns:
            df = self.calculate_ma(df)
        last = df.iloc[-1]
        return self.evaluate_exit_bar(
            direction=str(position.get("direction") or ""),
            close=float(last["close"]),
            ma25=float(last["ma25"]),
            trailing_active=True,
        )
