"""
MA Strategy Module — MA7/MA25/MA60 trading logic.
Each symbol gets its own MAStrategy instance so confirm_count is never shared.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MAStrategy:
    """Moving-average alignment strategy for a single trading symbol."""

    def __init__(self):
        self.confirm_count: int = 0
        self._last_direction: Optional[str] = None
        self._entry_time: Optional[datetime] = None  # used for time-based exit

    # ------------------------------------------------------------------
    # MA calculation
    # ------------------------------------------------------------------
    def calculate_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ma7, ma25, ma60 columns to *df* (in-place) and return it."""
        df = df.copy()
        df["ma7"] = df["close"].rolling(window=7, min_periods=1).mean()
        df["ma25"] = df["close"].rolling(window=25, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
        return df

    # ------------------------------------------------------------------
    # Three-line alignment
    # ------------------------------------------------------------------
    def check_alignment(self, df: pd.DataFrame) -> Optional[str]:
        """
        Return 'long', 'short', or None.

        Long  : close > ma7 > ma25 > ma60  AND  ma7/ma25 slope up for last 3 bars.
        Short : close < ma7 < ma25 < ma60  AND  ma7/ma25 slope down for last 3 bars.
        """
        if len(df) < 3:
            return None

        last = df.iloc[-1]
        close = last["close"]
        ma7 = last["ma7"]
        ma25 = last["ma25"]
        ma60 = last["ma60"]

        # Slope check: last 3 bars
        ma7_series = df["ma7"].iloc[-3:]
        ma25_series = df["ma25"].iloc[-3:]

        ma7_rising = all(ma7_series.iloc[i] < ma7_series.iloc[i + 1] for i in range(2))
        ma7_falling = all(ma7_series.iloc[i] > ma7_series.iloc[i + 1] for i in range(2))
        ma25_rising = all(ma25_series.iloc[i] < ma25_series.iloc[i + 1] for i in range(2))
        ma25_falling = all(ma25_series.iloc[i] > ma25_series.iloc[i + 1] for i in range(2))

        if close > ma7 > ma25 > ma60 and ma7_rising and ma25_rising:
            return "long"
        if close < ma7 < ma25 < ma60 and ma7_falling and ma25_falling:
            return "short"
        return None

    # ------------------------------------------------------------------
    # False breakout filter
    # ------------------------------------------------------------------
    def check_false_breakout(self, df: pd.DataFrame) -> bool:
        """
        Return True when the latest candle's wick is > 1.5× its body
        (indicating a possible false breakout — skip the signal).
        """
        if df.empty:
            return False
        last = df.iloc[-1]
        body = abs(last["close"] - last["open"])
        total_range = last["high"] - last["low"]
        if body == 0:
            return True  # doji — treat as false breakout
        wick = total_range - body
        return wick > body * 1.5

    # ------------------------------------------------------------------
    # Higher time-frame direction filter
    # ------------------------------------------------------------------
    def check_htf_direction(self, df_htf: pd.DataFrame) -> Optional[str]:
        """
        Return 'long', 'short', or None based on HTF MA alignment.
        Uses the same alignment logic without slope confirmation.
        """
        if df_htf is None or len(df_htf) < 60:
            return None
        df_htf = self.calculate_ma(df_htf)
        last = df_htf.iloc[-1]
        close = last["close"]
        ma7 = last["ma7"]
        ma25 = last["ma25"]
        ma60 = last["ma60"]
        if close > ma7 > ma25 > ma60:
            return "long"
        if close < ma7 < ma25 < ma60:
            return "short"
        return None

    # ------------------------------------------------------------------
    # Signal generation (with confirmation counter)
    # ------------------------------------------------------------------
    def get_signal(
        self,
        df_1m: pd.DataFrame,
        df_htf: Optional[pd.DataFrame],
        confirm_candles: int = 2,
    ) -> Optional[dict]:
        """
        Accumulate confirmation candles and return a signal dict when the
        threshold is reached, or None.

        Returns:
            {
                'direction': 'long' | 'short',
                'confirm':   <current count>,
                'max_confirm': <confirm_candles>,
            }
            or None if conditions are not met / confirmation not yet reached.
        """
        df_1m = self.calculate_ma(df_1m)

        direction = self.check_alignment(df_1m)
        if direction is None:
            # Reset counter
            self.confirm_count = 0
            self._last_direction = None
            return None

        # HTF filter — only skip if we have HTF data and it disagrees
        if df_htf is not None and len(df_htf) >= 60:
            htf_dir = self.check_htf_direction(df_htf)
            if htf_dir is not None and htf_dir != direction:
                self.confirm_count = 0
                self._last_direction = None
                return None

        # False breakout guard
        if self.check_false_breakout(df_1m):
            self.confirm_count = 0
            self._last_direction = None
            return None

        # Direction changed → reset counter
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

        # Not enough confirmations yet — return partial info so callers can
        # broadcast progress, but flag it as not actionable.
        return {**signal_info, "pending": True}

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------
    def check_exit(self, df: pd.DataFrame, position: dict) -> Optional[str]:
        """
        Return a close_reason string if the position should be closed, else None.

        Checks:
        1. MA60 cross (price crosses through MA60 against position direction).
        2. Time-based stop: position open for more than 5 minutes (for 1-min TF).
        """
        if df is None or df.empty:
            return None

        df = self.calculate_ma(df)
        last = df.iloc[-1]
        direction = position.get("direction")
        open_time = position.get("open_time")

        # --- MA60 cross exit ---
        ma60 = last["ma60"]
        close = last["close"]
        if direction == "long" and close < ma60:
            return "ma60_cross"
        if direction == "short" and close > ma60:
            return "ma60_cross"

        # --- Time stop (5 minutes) ---
        if open_time is not None:
            if isinstance(open_time, str):
                open_time = datetime.fromisoformat(open_time)
            elapsed = datetime.utcnow() - open_time
            if elapsed > timedelta(minutes=5):
                return "time_stop"

        return None
