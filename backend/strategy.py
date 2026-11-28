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
VALID_STRATEGY_MODES = {"balanced", "breakout", "classic", "ribbon"}
DEFAULT_COOLDOWN_BARS = 30
DEFAULT_FEE_RATE = 0.0004
DEFAULT_SLIPPAGE_RATE = 0.0001


class MAStrategy:
    """Moving-average alignment strategy for a single trading symbol."""

    def __init__(self, strategy_mode: str = STRATEGY_MODE_DEFAULT):
        self.confirm_count: int = 0
        self._last_direction: Optional[str] = None
        self._entry_time: Optional[datetime] = None
        self._ribbon_traded_cross_dir: Optional[str] = None
        self.strategy_mode: str = (
            strategy_mode if strategy_mode in VALID_STRATEGY_MODES else STRATEGY_MODE_DEFAULT
        )

    def set_strategy_mode(self, mode: str) -> None:
        norm = str(mode or "").strip().lower()
        if norm in VALID_STRATEGY_MODES and norm != self.strategy_mode:
            self.strategy_mode = norm
            self._ribbon_traded_cross_dir = None
            self.reset()

    def reset(self) -> None:
        self.confirm_count = 0
        self._last_direction = None

    def mark_ribbon_traded(self, direction: Optional[str]) -> None:
        if direction in ("long", "short"):
            self._ribbon_traded_cross_dir = direction

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
        if mode == "ribbon":
            return 0.9
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
        fast_momentum: bool = False,
        ltf_atr14: float = math.nan,
        ma25: float = math.nan,
    ) -> Tuple[float, float, float]:
        """
        Mode-aware initial stop-loss & reference take-profit:
          - classic : LTF MA60 or signal bar extreme, min 0.3%
          - ribbon  : MA25 structure buffer clamp(1.3 * |entry - MA25|, 0.45%, 1.0%)
          - fast_momentum (Plan A/B 1m waterfall override): compact ATR/structure stop in [0.45%, 0.75%]
          - balanced: HTF wave volatility stop max(2.0 * HTF_ATR14, 0.8% of entry)
          - breakout: HTF structure stop max(|entry - HTF_MA60|, 1.8 * HTF_ATR14, 0.8% of entry)
        """
        mode = cls.normalize_mode(strategy_mode)
        if mode == "classic":
            return cls.compute_sl_tp(direction, entry_price, ma60, candle_low, candle_high, rr_ratio=rr_ratio)

        if mode == "ribbon":
            d25 = 0.0
            if not math.isnan(ma25) and ma25 > 0:
                if direction == "long" and ma25 < entry_price:
                    d25 = (entry_price - ma25) * 1.3
                elif direction == "short" and ma25 > entry_price:
                    d25 = (ma25 - entry_price) * 1.3
            sl_distance = min(entry_price * 0.01, max(entry_price * 0.0045, d25))
        elif fast_momentum:
            la = 0.0 if (math.isnan(ltf_atr14) or ltf_atr14 <= 0) else float(ltf_atr14)
            ma60_dist = 0.0
            if not math.isnan(ma60) and ma60 > 0:
                if direction == "long" and ma60 < entry_price:
                    ma60_dist = entry_price - ma60
                elif direction == "short" and ma60 > entry_price:
                    ma60_dist = ma60 - entry_price
            base_fast = max(2.2 * la, entry_price * 0.0045)
            if 0 < ma60_dist <= entry_price * 0.0075:
                base_fast = max(base_fast, ma60_dist * 1.02)
            sl_distance = min(entry_price * 0.0075, base_fast)
        else:
            ha = 0.0 if (math.isnan(htf_atr14) or htf_atr14 <= 0) else float(htf_atr14)
            if mode == "balanced":
                sl_distance = max(2.0 * ha, entry_price * 0.008)
            else:  # breakout
                raw_d = 0.0
                if not math.isnan(htf_ma60) and htf_ma60 > 0:
                    raw_d = (entry_price - htf_ma60) if direction == "long" else (htf_ma60 - entry_price)
                sl_distance = min(entry_price * 0.014, max(raw_d, 1.8 * ha, entry_price * 0.008))

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

    @classmethod
    def compute_dynamic_ratchet_sl(
        cls,
        direction: str,
        entry_price: float,
        risk_r: float,
        current_stop_loss: float,
        best_price: float,
        strategy_mode: str = STRATEGY_MODE_DEFAULT,
        activate_r: float = TRAILING_ACTIVATE_R,
        breakeven_buffer: float = BREAKEVEN_BUFFER,
        live_price: Optional[float] = None,
    ) -> Tuple[float, bool]:
        """
        Dynamic R-ratchet profit-lock stop-loss as floating profit expands (Plan A & B):
          - peak_r >= eff_act_r (1.0R Plan A / 1.2R Plan B): locks +0.35R (floor ±0.2%)
          - peak_r >= 1.5R: locks +0.85R
          - peak_r >= 2.0R: continuously trails at max(0.85R, peak_r - 0.85R)
        Returns (new_stop_loss, is_activated).
        """
        if risk_r <= 0 or math.isnan(best_price) or best_price <= 0:
            return current_stop_loss, False

        mode = cls.normalize_mode(strategy_mode)
        eff_act_r = cls.effective_activate_r(mode, activate_r)
        peak_r = (
            (best_price - entry_price) / risk_r
            if direction == "long"
            else (entry_price - best_price) / risk_r
        )
        if peak_r < eff_act_r:
            return current_stop_loss, False

        if mode == "classic":
            sl = cls.compute_trailing_lock_sl(
                direction, entry_price, risk_r, current_stop_loss,
                activate_r=activate_r, breakeven_buffer=breakeven_buffer,
            )
            if live_price is not None and live_price > 0:
                sl = min(sl, live_price * 0.9995) if direction == "long" else max(sl, live_price * 1.0005)
                sl = max(current_stop_loss, sl) if direction == "long" else min(current_stop_loss, sl)
            return sl, True

        if mode == "ribbon":
            if peak_r >= 2.0:
                trail_gap = max(0.85 * risk_r, entry_price * 0.0085)
                locked_dist = max(0.75 * risk_r, abs(best_price - entry_price) - trail_gap)
            elif peak_r >= 1.4:
                locked_dist = 0.75 * risk_r
            else:
                locked_dist = max(0.25 * risk_r, entry_price * 0.0025)
        elif peak_r >= 2.0:
            cushion_pct = 0.010 if mode == "breakout" else 0.0085
            trail_gap = max(0.85 * risk_r, entry_price * cushion_pct)
            locked_dist = max(0.85 * risk_r, abs(best_price - entry_price) - trail_gap)
        elif peak_r >= 1.5:
            locked_dist = 0.85 * risk_r
        else:
            locked_dist = 0.35 * risk_r

        if direction == "long":
            desired = max(entry_price + locked_dist, entry_price * 1.002)
            desired = min(desired, best_price * 0.9995)
            if live_price is not None and live_price > 0:
                desired = min(desired, live_price * 0.9995)
            return max(current_stop_loss, desired), True

        desired = min(entry_price - locked_dist, entry_price * 0.998)
        desired = max(desired, best_price * 1.0005)
        if live_price is not None and live_price > 0:
            desired = max(desired, live_price * 1.0005)
        return min(current_stop_loss, desired), True

    @staticmethod
    def check_fast_momentum_scalars(
        o: float,
        c: float,
        ma7_seq: Tuple[float, float, float],
        ma25_seq: Tuple[float, float, float],
        ma60: float,
        atr14: float,
        htf_ma60: float,
        recent_opens: Tuple[float, ...],
        recent_closes: Tuple[float, ...],
        close_prev5: float,
        vol_recent5_avg: float,
        vol_ma20: float,
        strategy_mode: str = "balanced",
        htf_ma200: float = math.nan,
    ) -> Optional[str]:
        """
        1m Fast One-Way Momentum Override (captures sudden high-volume waterfalls/breakouts
        when slow 15m MAs haven't finished crossing yet, while still respecting HTF MA60 & MA200 price side):
          - Plan A (balanced): >=3.8*ATR & >=0.5% 5-bar move, >=2.0x 20-bar volume, 3-line LTF alignment
          - Plan B (breakout): >=2.8*ATR & >=0.4% 5-bar move, >=1.7x 20-bar volume, LTF MA7/MA25 alignment
          - Both require: >=3/4 directional 1m candles, current candle directional, and price beyond HTF MA60 (& MA200 if available).
        """
        m7_0, m7_1, m7_2 = ma7_seq
        m25_0, _m25_1, m25_2 = ma25_seq
        if any(
            math.isnan(v)
            for v in (o, c, m7_0, m7_1, m7_2, m25_0, m25_2, ma60, atr14, htf_ma60, close_prev5, vol_recent5_avg, vol_ma20)
        ):
            return None
        if atr14 <= 0 or vol_ma20 <= 0 or len(recent_opens) < 4 or len(recent_closes) < 4:
            return None

        mode = MAStrategy.normalize_mode(strategy_mode)
        if mode == "balanced":
            min_atr_mult = 3.8
            min_pct = 0.005
            min_vol_mult = 2.0
            max_bias_mult = 5.3
        elif mode == "breakout":
            min_atr_mult = 2.8
            min_pct = 0.004
            min_vol_mult = 1.7
            max_bias_mult = 4.5
        else:
            return None

        if vol_recent5_avg < min_vol_mult * vol_ma20:
            return None
        if abs(c - m25_2) > max_bias_mult * atr14:
            return None

        bull_bars = sum(1 for ro, rc in zip(recent_opens[-4:], recent_closes[-4:]) if rc > ro)
        bear_bars = sum(1 for ro, rc in zip(recent_opens[-4:], recent_closes[-4:]) if rc < ro)
        req_move = max(min_atr_mult * atr14, c * min_pct)

        # Fast LONG check (price broke above HTF MA60 lifeline with high-volume 1m burst)
        if c > o and bull_bars >= 3 and (c - close_prev5) >= req_move and c > htf_ma60:
            if mode == "balanced":
                if c > m7_2 > m25_2 > ma60 and m7_0 < m7_1 < m7_2 and m25_2 > m25_0:
                    return "long"
            else:
                if c > m7_2 > m25_2 and c > ma60 and m7_2 > m7_0 and m25_2 > m25_0:
                    return "long"

        # Fast SHORT check (price broke below HTF MA60 lifeline with high-volume 1m burst)
        if c < o and bear_bars >= 3 and (close_prev5 - c) >= req_move and c < htf_ma60:
            if mode == "balanced":
                if c < m7_2 < m25_2 < ma60 and m7_0 > m7_1 > m7_2 and m25_2 < m25_0:
                    return "short"
            else:
                if c < m7_2 < m25_2 and c < ma60 and m7_2 < m7_0 and m25_2 < m25_0:
                    return "short"

        return None

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

    @staticmethod
    def evaluate_ribbon_exit(direction: str, ma7: float, ma25: float) -> Optional[str]:
        """
        Plan C (ribbon) primary exit rule: close immediately when LTF MA7 crosses MA25
        against the open position (even if trailing profit-lock hasn't activated yet).
        """
        if math.isnan(ma7) or math.isnan(ma25):
            return None
        if direction == "long" and ma7 <= ma25:
            return "ma7_ma25_cross"
        if direction == "short" and ma7 >= ma25:
            return "ma7_ma25_cross"
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
        fast_dir: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Single-bar entry signal state machine (identical for live and backtest):
          - classic : 3-line alignment + 3-bar MA7/MA25 slope, non-opposing HTF, ATR14 bias, adverse wick
          - ribbon  : N consecutive bars on MA7 single side without crossing MA25 + expanding ribbon + ATR>=0.09% + non-opposing HTF
          - balanced: STRICT HTF trend required (or fast_dir 1m burst override) + LTF 3-line alignment
          - breakout: STRICT HTF strong-spread trend (or fast_dir 1m burst override) + LTF breakout
        """
        mode = self.normalize_mode(strategy_mode or self.strategy_mode)
        m7_0, _m7_1, m7_2 = ma7_seq
        m25_0, _m25_1, m25_2 = ma25_seq
        used_fast_override = False

        if mode == "ribbon":
            if any(math.isnan(v) for v in (o, h, l, c, m7_0, m7_2, m25_0, m25_2, atr14)) or c <= 0:
                self.reset()
                return None
            cur_cross_dir = "long" if m7_2 > m25_2 else ("short" if m7_2 < m25_2 else None)
            if self._ribbon_traded_cross_dir is not None and cur_cross_dir != self._ribbon_traded_cross_dir:
                self._ribbon_traded_cross_dir = None

            if c > m7_2 and m7_2 > m25_2 and l > m25_2:
                direction = "long"
            elif c < m7_2 and m7_2 < m25_2 and h < m25_2:
                direction = "short"
            else:
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

            # Entry-readiness filters on confirmed ribbon streak (without resetting streak count)
            if self._ribbon_traded_cross_dir == direction:
                return {**signal_info, "pending": True}
            if atr14 / c < 0.0009:
                return {**signal_info, "pending": True}
            if abs(m7_2 - m25_2) <= abs(m7_0 - m25_0):
                return {**signal_info, "pending": True}
            if direction == "long" and not (m7_2 > m7_0 and m25_2 > m25_0):
                return {**signal_info, "pending": True}
            if direction == "short" and not (m7_2 < m7_0 and m25_2 < m25_0):
                return {**signal_info, "pending": True}
            if self.check_overextended_scalars(c, m25_2, atr14):
                return {**signal_info, "pending": True}
            if htf_dir is not None and htf_dir != direction:
                return {**signal_info, "pending": True}
            return signal_info

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
            if any(math.isnan(v) for v in (o, h, l, c, m7_0, m7_2, m25_2, ma60)):
                self.reset()
                return None

            direction = None
            if htf_dir in ("long", "short"):
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

            # Fast 1m one-way momentum override when slow 15m MAs haven't crossed yet
            if direction is None and fast_dir in ("long", "short"):
                direction = fast_dir
                used_fast_override = True

            if direction is None:
                self.reset()
                return None

        if direction != self._last_direction:
            self.confirm_count = 0
            self._last_direction = direction

        eff_confirm = min(confirm_candles, 2) if used_fast_override else confirm_candles
        self.confirm_count = min(self.confirm_count + 1, confirm_candles)

        signal_info = {
            "direction": direction,
            "confirm": self.confirm_count,
            "max_confirm": eff_confirm,
        }
        if used_fast_override:
            signal_info["fast_momentum"] = True

        if self.confirm_count < eff_confirm:
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
        if mode in ("classic", "ribbon"):
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
        htf_ma60 = math.nan
        htf_ma200 = math.nan
        htf_atr14 = math.nan
        if df_htf is not None and len(df_htf) >= 60:
            df_htf_calc = self.calculate_ma(df_htf, include_ma200=(mode not in ("classic", "ribbon")))
            htf_last = df_htf_calc.iloc[-1]
            htf_ma25 = float(htf_last["ma25"])
            htf_ma60 = float(htf_last["ma60"])
            htf_ma200 = float(htf_last["ma200"]) if "ma200" in df_htf_calc.columns else math.nan
            htf_atr14 = float(htf_last["atr14"])
            if mode in ("classic", "ribbon"):
                htf_dir = self.check_htf_scalars(
                    float(htf_last["close"]), float(htf_last["ma7"]), htf_ma25, htf_ma60
                )
            elif len(df_htf_calc) >= 62:
                prev2 = df_htf_calc.iloc[-3]
                htf_dir = self.check_htf_mode_scalars(
                    ltf_close=ltf_close,
                    htf_close=float(htf_last["close"]),
                    htf_ma7=float(htf_last["ma7"]),
                    htf_ma25=htf_ma25,
                    htf_ma25_prev2=float(prev2["ma25"]),
                    htf_ma60=htf_ma60,
                    htf_ma60_prev2=float(prev2["ma60"]),
                    htf_ma200=htf_ma200,
                    htf_atr14=htf_atr14,
                    strategy_mode=mode,
                )

        ma7_s = df_1m["ma7"].iloc[-3:]
        ma25_s = df_1m["ma25"].iloc[-3:]
        ma7_tup = (float(ma7_s.iloc[0]), float(ma7_s.iloc[1]), float(ma7_s.iloc[2]))
        ma25_tup = (float(ma25_s.iloc[0]), float(ma25_s.iloc[1]), float(ma25_s.iloc[2]))

        fast_dir = None
        if mode in ("balanced", "breakout") and len(df_1m) >= 25 and "volume" in df_1m.columns:
            vol_s = df_1m["volume"]
            fast_dir = self.check_fast_momentum_scalars(
                o=float(last["open"]),
                c=ltf_close,
                ma7_seq=ma7_tup,
                ma25_seq=ma25_tup,
                ma60=float(last["ma60"]),
                atr14=float(last["atr14"]),
                htf_ma60=htf_ma60,
                recent_opens=tuple(float(x) for x in df_1m["open"].iloc[-4:]),
                recent_closes=tuple(float(x) for x in df_1m["close"].iloc[-4:]),
                close_prev5=float(df_1m["close"].iloc[-6]),
                vol_recent5_avg=float(vol_s.iloc[-5:].mean()),
                vol_ma20=float(vol_s.iloc[-25:-5].mean()),
                strategy_mode=mode,
                htf_ma200=htf_ma200,
            )

        return self.evaluate_bar(
            o=float(last["open"]),
            h=float(last["high"]),
            l=float(last["low"]),
            c=ltf_close,
            ma7_seq=ma7_tup,
            ma25_seq=ma25_tup,
            ma60=float(last["ma60"]),
            atr14=float(last["atr14"]),
            htf_dir=htf_dir,
            confirm_candles=confirm_candles,
            strategy_mode=mode,
            htf_ma25=htf_ma25,
            htf_atr14=htf_atr14,
            fast_dir=fast_dir,
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
