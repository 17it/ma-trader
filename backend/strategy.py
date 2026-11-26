"""
MA Strategy Module — MA7/MA25/MA60 trend alignment + ATR(14) adaptive bias +
adverse-wick entry filter + 1.2R break-even & MA25 trailing exit.

Single source of truth shared by both live/paper TradingBot and Backtester.
Each symbol gets its own MAStrategy instance so confirm_count is never shared.
"""

import logging
import math
import sys
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

sys.modules.setdefault("strategy", sys.modules[__name__])
sys.modules.setdefault("backend.strategy", sys.modules[__name__])

logger = logging.getLogger(__name__)

ATR_PERIOD = 14
ATR_MA25_BIAS_MULT = 2.5
WICK_BODY_MULT = 1.5
TRAILING_ACTIVATE_R = 0.88
BREAKEVEN_BUFFER = 0.003  # +0.3% / -0.3% profit-lock buffer
TRAILING_MA_DEFAULT = "ma25"
VALID_TRAILING_MAS = {"ma25", "ma60"}
STRATEGY_MODE_DEFAULT = "classic"
VALID_STRATEGY_MODES = {"balanced", "breakout", "classic"}
DEFAULT_COOLDOWN_BARS = 30
DEFAULT_FEE_RATE = 0.0004
DEFAULT_SLIPPAGE_RATE = 0.0001


class MAStrategy:
    """Moving-average alignment strategy for a single trading symbol."""

    def __init__(self, strategy_mode: str = STRATEGY_MODE_DEFAULT):
        self.confirm_count: int = 0
        self._last_direction: Optional[str] = None
        self._entry_time: Optional[datetime] = None
        self.strategy_mode: str = (
            strategy_mode if strategy_mode in VALID_STRATEGY_MODES else STRATEGY_MODE_DEFAULT
        )

    def set_strategy_mode(self, mode: str) -> None:
        norm = str(mode or "").strip().lower()
        if norm in VALID_STRATEGY_MODES and norm != self.strategy_mode:
            self.strategy_mode = norm
            self.reset()

    def reset(self) -> None:
        self.confirm_count = 0
        self._last_direction = None

    @staticmethod
    def normalize_mode(mode: Optional[str]) -> str:
        norm = str(mode or "").strip().lower()
        return norm if norm in VALID_STRATEGY_MODES else STRATEGY_MODE_DEFAULT

    @staticmethod
    def effective_activate_r(strategy_mode: Optional[str], configured_r: float = TRAILING_ACTIVATE_R) -> float:
        mode = MAStrategy.normalize_mode(strategy_mode)
        if mode == "balanced":
            return 1.0
        if mode == "breakout":
            return 1.2
        return float(configured_r)

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

    @classmethod
    def compute_mode_sl_tp(
        cls,
        direction: str,
        entry_price: float,
        ma60: float,
        candle_low: float,
        candle_high: float,
        rr_ratio: float = 1.5,
        strategy_mode: str = STRATEGY_MODE_DEFAULT,
        htf_atr14: float = math.nan,
        htf_ma60: float = math.nan,
    ) -> Tuple[float, float, float]:
        """
        Mode-aware initial stop-loss & reference take-profit:
          - classic : LTF MA60 or signal bar extreme, min 0.3%
          - balanced: HTF wave volatility stop max(2.0 * HTF_ATR14, 0.8% of entry)
          - breakout: HTF structure stop max(|entry - HTF_MA60|, 1.8 * HTF_ATR14, 0.8% of entry)
        """
        mode = cls.normalize_mode(strategy_mode)
        if mode == "classic":
            return cls.compute_sl_tp(direction, entry_price, ma60, candle_low, candle_high, rr_ratio=rr_ratio)

        ha = 0.0 if (math.isnan(htf_atr14) or htf_atr14 <= 0) else float(htf_atr14)
        if mode == "balanced":
            sl_distance = max(2.0 * ha, entry_price * 0.008)
        else:  # breakout
            raw_d = 0.0
            if not math.isnan(htf_ma60) and htf_ma60 > 0:
                raw_d = (entry_price - htf_ma60) if direction == "long" else (htf_ma60 - entry_price)
            sl_distance = max(raw_d, 1.8 * ha, entry_price * 0.008)

        if direction == "long":
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + sl_distance * rr_ratio
        else:
            stop_loss = entry_price + sl_distance
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

    @classmethod
    def compute_mode_trailing_lock_sl(
        cls,
        direction: str,
        entry_price: float,
        risk_r: float,
        current_stop_loss: float,
        strategy_mode: str = STRATEGY_MODE_DEFAULT,
        activate_r: float = TRAILING_ACTIVATE_R,
        breakeven_buffer: float = BREAKEVEN_BUFFER,
    ) -> float:
        """
        Mode-aware profit-lock stop when trailing activates:
          - classic : entry ± breakeven_buffer
          - balanced / breakout: locks +0.35R (floor ±0.2% of entry), safely capped inside trigger price
        """
        mode = cls.normalize_mode(strategy_mode)
        if mode == "classic":
            return cls.compute_trailing_lock_sl(
                direction, entry_price, risk_r, current_stop_loss,
                activate_r=activate_r, breakeven_buffer=breakeven_buffer,
            )
        eff_act_r = cls.effective_activate_r(mode, activate_r)
        if direction == "long":
            trigger_price = entry_price + eff_act_r * risk_r
            desired = max(entry_price + 0.35 * risk_r, entry_price * 1.002)
            return max(current_stop_loss, min(desired, trigger_price * 0.9995))
        trigger_price = entry_price - eff_act_r * risk_r
        desired = min(entry_price - 0.35 * risk_r, entry_price * 0.998)
        return min(current_stop_loss, max(desired, trigger_price * 1.0005))

    # ------------------------------------------------------------------
    # Indicator calculation (MA7 / MA25 / MA60 + ATR14 + optional MA200)
    # ------------------------------------------------------------------
    def calculate_ma(self, df: pd.DataFrame, include_ma200: bool = False) -> pd.DataFrame:
        """Add ma7, ma25, ma60, atr14 (and optional ma200) columns to a copy of *df* and return it."""
        df = df.copy()
        df["ma7"] = df["close"].rolling(window=7, min_periods=1).mean()
        df["ma25"] = df["close"].rolling(window=25, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()
        if include_ma200:
            df["ma200"] = df["close"].rolling(window=200, min_periods=200).mean()

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

    @classmethod
    def check_htf_mode_scalars(
        cls,
        ltf_close: float,
        htf_close: float,
        htf_ma7: float,
        htf_ma25: float,
        htf_ma25_prev2: float,
        htf_ma60: float,
        htf_ma60_prev2: float,
        htf_ma200: float = math.nan,
        htf_atr14: float = math.nan,
        strategy_mode: str = STRATEGY_MODE_DEFAULT,
    ) -> Optional[str]:
        """
        Mode-aware HTF gate:
          - classic : MA7 > MA25 > MA60 (or reverse)
          - balanced: strict MA7/25/60 alignment + 2-bar MA25/MA60 slope + HTF MA200 macro filter
          - breakout: balanced + strong MA spread (|MA7 - MA60| > 0.6 * HTF_ATR14) & |ltf_close - HTF_MA25| <= 2.2 * HTF_ATR14
        """
        mode = cls.normalize_mode(strategy_mode)
        if mode == "classic":
            return cls.check_htf_scalars(htf_close, htf_ma7, htf_ma25, htf_ma60)

        if any(math.isnan(v) for v in (htf_close, htf_ma7, htf_ma25, htf_ma25_prev2, htf_ma60, htf_ma60_prev2)):
            return None

        h_long = (htf_ma7 > htf_ma25 > htf_ma60) and (htf_ma25 > htf_ma25_prev2) and (htf_ma60 > htf_ma60_prev2)
        h_short = (htf_ma7 < htf_ma25 < htf_ma60) and (htf_ma25 < htf_ma25_prev2) and (htf_ma60 < htf_ma60_prev2)

        if not math.isnan(htf_ma200):
            h_long = h_long and (htf_close > htf_ma200) and (htf_ma60 > htf_ma200)
            h_short = h_short and (htf_close < htf_ma200) and (htf_ma60 < htf_ma200)

        if mode == "breakout" and not math.isnan(htf_atr14) and htf_atr14 > 0:
            spread_ok = abs(htf_ma7 - htf_ma60) > 0.6 * htf_atr14
            bias_ok = math.isnan(ltf_close) or (abs(ltf_close - htf_ma25) <= 2.2 * htf_atr14)
            h_long = h_long and spread_ok and bias_ok
            h_short = h_short and spread_ok and bias_ok

        if h_long:
            return "long"
        if h_short:
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
        ma_val: float,
        trailing_active: bool,
        trailing_ma: str = TRAILING_MA_DEFAULT,
        is_htf_rail: bool = False,
    ) -> Optional[str]:
        """
        Indicator-based bar-close exit rule shared by live and backtest:
          - Before trailing activates: NO premature MA exit.
          - After trailing activates: exit when a candle closes beyond the configured trailing MA.
        """
        if not trailing_active or math.isnan(close) or math.isnan(ma_val):
            return None
        ma_key = str(trailing_ma or TRAILING_MA_DEFAULT).lower()
        prefix = "htf_" if is_htf_rail else ""
        reason = f"{prefix}ma60_trailing" if ma_key == "ma60" else f"{prefix}ma25_trailing"
        if direction == "long" and close < ma_val:
            return reason
        if direction == "short" and close > ma_val:
            return reason
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
        strategy_mode: Optional[str] = None,
        htf_ma25: float = math.nan,
        htf_atr14: float = math.nan,
    ) -> Optional[dict]:
        """
        Single-bar entry signal state machine (identical for live and backtest):
          - classic : 3-line alignment + 3-bar MA7/MA25 slope, non-opposing HTF, ATR14 bias, adverse wick
          - balanced: STRICT HTF trend required + LTF 3-line alignment & MA7 2-bar slope + ATR14 bias + adverse wick
          - breakout: STRICT HTF strong-spread trend + LTF close & MA7 above/below MA25 + directional body + HTF bias + adverse wick
        """
        mode = self.normalize_mode(strategy_mode or self.strategy_mode)
        m7_0, _m7_1, m7_2 = ma7_seq
        _m25_0, _m25_1, m25_2 = ma25_seq

        if mode == "classic":
            direction = self.check_alignment_scalars(c, ma7_seq, ma25_seq, ma60)
            if direction is None:
                self.reset()
                return None
            if htf_dir is not None and htf_dir != direction:
                self.reset()
                return None
            if self.check_overextended_scalars(c, m25_2, atr14):
                self.reset()
                return None
        else:
            # Both Plan A (balanced) and Plan B (breakout) STRICTLY require confirmed HTF trend (no chop entries!)
            if htf_dir not in ("long", "short"):
                self.reset()
                return None
            if any(math.isnan(v) for v in (o, h, l, c, m7_0, m7_2, m25_2, ma60)):
                self.reset()
                return None

            direction = None
            if mode == "balanced":
                if htf_dir == "long" and (c > m7_2 > m25_2 > ma60) and (m7_2 > m7_0) and not self.check_overextended_scalars(c, m25_2, atr14):
                    direction = "long"
                elif htf_dir == "short" and (c < m7_2 < m25_2 < ma60) and (m7_2 < m7_0) and not self.check_overextended_scalars(c, m25_2, atr14):
                    direction = "short"
            else:  # breakout (Plan B)
                htf_bias_ok = (
                    True
                    if (math.isnan(htf_ma25) or math.isnan(htf_atr14) or htf_atr14 <= 0)
                    else (abs(c - htf_ma25) <= 1.8 * htf_atr14)
                )
                if htf_dir == "long" and (c > m25_2) and (m7_2 > m25_2) and (c > o) and htf_bias_ok:
                    direction = "long"
                elif htf_dir == "short" and (c < m25_2) and (m7_2 < m25_2) and (c < o) and htf_bias_ok:
                    direction = "short"

            if direction is None:
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

    def check_htf_direction(
        self,
        df_htf: pd.DataFrame,
        ltf_close: float = math.nan,
        strategy_mode: Optional[str] = None,
    ) -> Optional[str]:
        if df_htf is None or len(df_htf) < 60:
            return None
        mode = self.normalize_mode(strategy_mode or self.strategy_mode)
        if mode == "classic":
            df_htf = self.calculate_ma(df_htf)
            last = df_htf.iloc[-1]
            return self.check_htf_scalars(
                float(last["close"]), float(last["ma7"]), float(last["ma25"]), float(last["ma60"])
            )
        if len(df_htf) < 62:
            return None
        df_htf = self.calculate_ma(df_htf, include_ma200=True)
        last = df_htf.iloc[-1]
        prev2 = df_htf.iloc[-3]
        return self.check_htf_mode_scalars(
            ltf_close=ltf_close,
            htf_close=float(last["close"]),
            htf_ma7=float(last["ma7"]),
            htf_ma25=float(last["ma25"]),
            htf_ma25_prev2=float(prev2["ma25"]),
            htf_ma60=float(last["ma60"]),
            htf_ma60_prev2=float(prev2["ma60"]),
            htf_ma200=float(last["ma200"]) if "ma200" in df_htf.columns else math.nan,
            htf_atr14=float(last["atr14"]),
            strategy_mode=mode,
        )

    def get_signal(
        self,
        df_1m: pd.DataFrame,
        df_htf: Optional[pd.DataFrame],
        confirm_candles: int = 2,
        strategy_mode: Optional[str] = None,
    ) -> Optional[dict]:
        if df_1m is None or len(df_1m) < 3:
            self.reset()
            return None
        mode = self.normalize_mode(strategy_mode or self.strategy_mode)
        df_1m = self.calculate_ma(df_1m)
        last = df_1m.iloc[-1]
        ltf_close = float(last["close"])

        htf_dir = None
        htf_ma25 = math.nan
        htf_atr14 = math.nan
        if df_htf is not None and len(df_htf) >= 60:
            df_htf_calc = self.calculate_ma(df_htf, include_ma200=(mode != "classic"))
            htf_last = df_htf_calc.iloc[-1]
            htf_ma25 = float(htf_last["ma25"])
            htf_atr14 = float(htf_last["atr14"])
            if mode == "classic":
                htf_dir = self.check_htf_scalars(
                    float(htf_last["close"]), float(htf_last["ma7"]), htf_ma25, float(htf_last["ma60"])
                )
            elif len(df_htf_calc) >= 62:
                prev2 = df_htf_calc.iloc[-3]
                htf_dir = self.check_htf_mode_scalars(
                    ltf_close=ltf_close,
                    htf_close=float(htf_last["close"]),
                    htf_ma7=float(htf_last["ma7"]),
                    htf_ma25=htf_ma25,
                    htf_ma25_prev2=float(prev2["ma25"]),
                    htf_ma60=float(htf_last["ma60"]),
                    htf_ma60_prev2=float(prev2["ma60"]),
                    htf_ma200=float(htf_last["ma200"]) if "ma200" in df_htf_calc.columns else math.nan,
                    htf_atr14=htf_atr14,
                    strategy_mode=mode,
                )

        ma7_s = df_1m["ma7"].iloc[-3:]
        ma25_s = df_1m["ma25"].iloc[-3:]
        return self.evaluate_bar(
            o=float(last["open"]),
            h=float(last["high"]),
            l=float(last["low"]),
            c=ltf_close,
            ma7_seq=(float(ma7_s.iloc[0]), float(ma7_s.iloc[1]), float(ma7_s.iloc[2])),
            ma25_seq=(float(ma25_s.iloc[0]), float(ma25_s.iloc[1]), float(ma25_s.iloc[2])),
            ma60=float(last["ma60"]),
            atr14=float(last["atr14"]),
            htf_dir=htf_dir,
            confirm_candles=confirm_candles,
            strategy_mode=mode,
            htf_ma25=htf_ma25,
            htf_atr14=htf_atr14,
        )

    def check_exit(self, df: pd.DataFrame, position: dict, is_htf_rail: bool = False) -> Optional[str]:
        if df is None or df.empty:
            return None
        if not bool(position.get("trailing_active", False)):
            return None
        trailing_ma = str(position.get("trailing_ma") or TRAILING_MA_DEFAULT).lower()
        if trailing_ma not in VALID_TRAILING_MAS:
            trailing_ma = TRAILING_MA_DEFAULT
        if trailing_ma not in df.columns:
            df = self.calculate_ma(df)
        last = df.iloc[-1]
        return self.evaluate_exit_bar(
            direction=str(position.get("direction") or ""),
            close=float(last["close"]),
            ma_val=float(last[trailing_ma]),
            trailing_active=True,
            trailing_ma=trailing_ma,
            is_htf_rail=is_htf_rail,
        )
