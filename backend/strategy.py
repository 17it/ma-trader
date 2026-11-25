"""
MA Strategy Module — MA7/MA25/MA60 trading logic.
Single source of truth shared by both live/paper TradingBot and Backtester.
Each symbol gets its own MAStrategy instance so confirm_count is never shared.
"""

import logging
import math
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------
    # MA calculation
    # ------------------------------------------------------------------
    def calculate_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ma7, ma25, ma60 columns to a copy of *df* and return it."""
        df = df.copy()
        df["ma7"] = df["close"].rolling(window=7, min_periods=1).mean()
        df["ma25"] = df["close"].rolling(window=25, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
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
        if any(math.isnan(v) for v in (close, ma7, ma25, ma60)):
            return None
        if close > ma7 > ma25 > ma60:
            return "long"
        if close < ma7 < ma25 < ma60:
            return "short"
        return None

    @staticmethod
    def check_false_breakout_scalars(o: float, h: float, l: float, c: float) -> bool:
        body = abs(c - o)
        total_range = h - l
        if body == 0:
            return True  # doji
        wick = total_range - body
        return wick > body * 1.5

    @staticmethod
    def check_overextended_scalars(
        close: float,
        ma25: float,
        ma60: float,
        max_ma25_bias: float = 0.0035,
        max_ma60_bias: float = 0.0060,
    ) -> bool:
        if ma25 > 0 and abs(close - ma25) / ma25 > max_ma25_bias:
            return True
        if ma60 > 0 and abs(close - ma60) / ma60 > max_ma60_bias:
            return True
        return False

    @staticmethod
    def evaluate_exit_bar(
        direction: str,
        close: float,
        ma7: float,
        ma60: float,
        trailing_active: bool,
    ) -> Optional[str]:
        """Indicator-based bar-close exit rule shared by live and backtest."""
        if math.isnan(close) or math.isnan(ma7) or math.isnan(ma60):
            return None
        if trailing_active:
            if direction == "long" and close < ma7:
                return "ma7_trailing"
            if direction == "short" and close > ma7:
                return "ma7_trailing"
        else:
            if direction == "long" and close < ma60:
                return "ma60_cross"
            if direction == "short" and close > ma60:
                return "ma60_cross"
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
        htf_dir: Optional[str],
        confirm_candles: int = 2,
    ) -> Optional[dict]:
        """
        Single-bar entry signal state machine (identical for live and backtest).
        Returns:
            - None if conditions are not met (counter reset)
            - {'direction', 'confirm', 'max_confirm', 'pending': True} while confirming
            - {'direction', 'confirm', 'max_confirm'} once confirm_count >= confirm_candles
        """
        direction = self.check_alignment_scalars(c, ma7_seq, ma25_seq, ma60)
        if direction is None:
            self.reset()
            return None

        # HTF filter — skip if HTF has a clear opposing trend
        if htf_dir is not None and htf_dir != direction:
            self.reset()
            return None

        # False breakout guard
        if self.check_false_breakout_scalars(o, h, l, c):
            self.reset()
            return None

        # Overextension / anti-chasing guard
        if self.check_overextended_scalars(c, ma25_seq[2], ma60):
            self.reset()
            return None

        if direction != self._last_direction:
            self.confirm_count = 0
            self._last_direction = direction

        self.confirm_count += 1

        signal_info = {
            "direction": direction,
            "confirm": self.confirm_count,
            "max_confirm": confirm_candles,
        }
        if self.confirm_count >= confirm_candles:
            return signal_info
        return {**signal_info, "pending": True}

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

    def check_false_breakout(self, df: pd.DataFrame) -> bool:
        if df.empty:
            return False
        last = df.iloc[-1]
        return self.check_false_breakout_scalars(
            float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
        )

    def check_overextended(
        self,
        df: pd.DataFrame,
        max_ma25_bias: float = 0.0035,
        max_ma60_bias: float = 0.0060,
    ) -> bool:
        if df.empty:
            return False
        last = df.iloc[-1]
        return self.check_overextended_scalars(
            float(last["close"]), float(last["ma25"]), float(last["ma60"]), max_ma25_bias, max_ma60_bias
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
            htf_dir=htf_dir,
            confirm_candles=confirm_candles,
        )

    def check_exit(self, df: pd.DataFrame, position: dict) -> Optional[str]:
        if df is None or df.empty:
            return None
        df = self.calculate_ma(df)
        last = df.iloc[-1]
        return self.evaluate_exit_bar(
            direction=str(position.get("direction") or ""),
            close=float(last["close"]),
            ma7=float(last["ma7"]),
            ma60=float(last["ma60"]),
            trailing_active=bool(position.get("trailing_active", False)),
        )
