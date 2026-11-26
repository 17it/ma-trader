"""
TradingBot — multi-symbol async monitoring loop.

Each symbol runs in its own asyncio.Task aligned to candle-close boundaries.
Strategy signals trigger trade execution (real or paper) via *executor*,
exchange stop-loss synchronization, exchange position reconciliation, and
notifications via *notifier*.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

try:
    from backend.strategy import BREAKEVEN_BUFFER, MAStrategy, TRAILING_ACTIVATE_R
    from backend.binance_ws import BinanceWSStreamer
except ImportError:
    from strategy import BREAKEVEN_BUFFER, MAStrategy, TRAILING_ACTIVATE_R
    from binance_ws import BinanceWSStreamer

logger = logging.getLogger(__name__)

# Maximum polling interval per timeframe (seconds); actual sleep also aligns to bar-close + 2s.
_TF_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}

_TF_MAX_POLL: Dict[str, int] = {
    "1m": 15,
    "3m": 25,
    "5m": 30,
    "15m": 45,
    "30m": 60,
    "1h": 60,
}
_DEFAULT_MAX_POLL = 60


def _sleep_until_next_poll(timeframe: str) -> float:
    """
    Compute a sleep duration that wakes up ~2s after the next candle closes
    while still polling periodically for UI MA updates.
    """
    tf_sec = _TF_SECONDS.get(timeframe, 60)
    max_poll = _TF_MAX_POLL.get(timeframe, _DEFAULT_MAX_POLL)
    now = time.time()
    sec_into_bar = now % tf_sec
    to_bar_close = tf_sec - sec_into_bar + 2.0
    return max(2.0, min(float(max_poll), to_bar_close))


def _config_signature(cfg) -> tuple:
    return (
        getattr(cfg, "timeframe", "1m"),
        getattr(cfg, "htf_timeframe", "15m"),
        getattr(cfg, "confirm_candles", 2),
        getattr(cfg, "leverage", 5),
        getattr(cfg, "risk_per_trade", 0.01),
        getattr(cfg, "rr_ratio", 1.5),
        getattr(cfg, "trailing_activate_r", TRAILING_ACTIVATE_R),
        getattr(cfg, "breakeven_buffer", BREAKEVEN_BUFFER),
    )


class TradingBot:
    """Manages concurrent monitoring of multiple trading symbols."""

    def __init__(
        self,
        db_session_factory,
        data_feed,
        executor,
        notifier,
        ws_streamer: Optional[BinanceWSStreamer] = None,
        is_paper: Optional[bool] = None,
    ):
        self.db_session_factory = db_session_factory
        self.data_feed = data_feed
        self.executor = executor
        self.notifier = notifier
        if is_paper is not None:
            self.is_paper = bool(is_paper)
        elif executor is not None and hasattr(executor, "is_paper"):
            self.is_paper = bool(executor.is_paper)
        else:
            self.is_paper = True

        self.running: bool = False
        self._started_at: Optional[float] = None
        self.strategies: Dict[str, MAStrategy] = {}
        self.positions: Dict[str, dict] = {}
        self.confirm_counts: Dict[str, int] = {}
        self.ws_callbacks: List[Callable[[dict], None]] = []

        self._tasks: Dict[str, asyncio.Task] = {}
        self._config_sigs: Dict[str, tuple] = {}
        self._exit_task: Optional[asyncio.Task] = None
        self._exit_cycle_count: int = 0

        if ws_streamer is not None:
            self.ws_streamer = ws_streamer
            self._owns_ws_streamer = False
        else:
            self.ws_streamer = BinanceWSStreamer()
            self.ws_streamer.register_callback(lambda msg: self.broadcast(msg))
            self._owns_ws_streamer = True
        if hasattr(self.data_feed, "set_ws_streamer"):
            self.data_feed.set_ws_streamer(self.ws_streamer)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        """Load enabled symbol configs from DB and start monitoring tasks."""
        if self.running:
            logger.warning("TradingBot.start() called while already running")
            return

        self.running = True
        self._started_at = time.time()
        self.broadcast({"type": "bot_status", "running": True})
        self.broadcast({"type": "log", "level": "info", "message": "Bot started"})

        symbol_configs = self._load_symbol_configs()
        rr_by_symbol = {c.symbol: getattr(c, "rr_ratio", 1.5) for c in symbol_configs}
        trail_r_by_symbol = {
            c.symbol: float(getattr(c, "trailing_activate_r", None) or TRAILING_ACTIVATE_R)
            for c in symbol_configs
        }
        be_buf_by_symbol = {
            c.symbol: float(
                getattr(c, "breakeven_buffer")
                if getattr(c, "breakeven_buffer", None) is not None
                else BREAKEVEN_BUFFER
            )
            for c in symbol_configs
        }

        active_symbols = [c.symbol for c in symbol_configs if getattr(c, "symbol", None)]
        if not getattr(self.ws_streamer, "_running", False):
            self.ws_streamer.start(active_symbols)
        else:
            self.ws_streamer.update_symbols(active_symbols)

        try:
            try:
                from backend.database import record_operation_log
            except ImportError:
                from database import record_operation_log
            mode_str = "paper" if self.is_paper else "live"
            mode_label = "模拟交易" if self.is_paper else "实盘交易"
            syms_label = ", ".join(active_symbols) if active_symbols else "无"
            record_operation_log(
                log_type="start",
                title="系统启动",
                message=f"交易系统已启动（{mode_label}），监控交易对：{syms_label}",
                level="info",
                mode=mode_str,
            )
            self.broadcast({"type": "operation_log"})
        except Exception:
            pass

        # Restore open trades from DB into memory (strictly scoped by is_paper)
        try:
            try:
                from backend.database import Trade
            except ImportError:
                from database import Trade
            session = self.db_session_factory()
            try:
                open_trades = session.query(Trade).filter_by(status="open", is_paper=self.is_paper).all()
                for ot in open_trades:
                    saved_risk = getattr(ot, "initial_risk", None)
                    if not saved_risk or saved_risk <= 0:
                        if (ot.direction == "long" and ot.stop_loss < ot.entry_price) or (
                            ot.direction == "short" and ot.stop_loss > ot.entry_price
                        ):
                            saved_risk = abs(ot.entry_price - ot.stop_loss)
                        else:
                            sym_rr = rr_by_symbol.get(ot.symbol, 1.5) or 1.5
                            saved_risk = abs(ot.take_profit - ot.entry_price) / sym_rr
                    self.positions[ot.symbol] = {
                        "id": ot.id,
                        "symbol": ot.symbol,
                        "direction": ot.direction,
                        "entry_price": ot.entry_price,
                        "stop_loss": ot.stop_loss,
                        "take_profit": ot.take_profit,
                        "quantity": ot.quantity,
                        "status": "open",
                        "is_paper": ot.is_paper,
                        "open_time": ot.open_time,
                        "timeframe": ot.timeframe or "1m",
                        "exchange_order_id": getattr(ot, "exchange_order_id", None),
                        "sl_order_id": getattr(ot, "sl_order_id", None),
                        "trailing_active": bool(getattr(ot, "trailing_active", False)),
                        "risk_r": saved_risk,
                        "trailing_activate_r": (
                            float(ot.trailing_activate_r)
                            if getattr(ot, "trailing_activate_r", None) is not None
                            else trail_r_by_symbol.get(ot.symbol, TRAILING_ACTIVATE_R)
                        ),
                        "breakeven_buffer": (
                            float(ot.breakeven_buffer)
                            if getattr(ot, "breakeven_buffer", None) is not None
                            else be_buf_by_symbol.get(ot.symbol, BREAKEVEN_BUFFER)
                        ),
                    }
                if open_trades:
                    logger.info("Restored %d open trades from database into memory", len(open_trades))
            finally:
                session.close()
        except Exception as exc:
            logger.warning("Failed to restore open trades on start: %s", exc)

        for config in symbol_configs:
            await self._start_symbol(config)

        self._exit_task = asyncio.create_task(self._exit_monitor(), name="exit-monitor")
        logger.info("TradingBot started with %d symbols (is_paper=%s)", len(symbol_configs), self.is_paper)

    async def stop(self):
        """Cancel all monitoring tasks and mark bot as stopped."""
        self.running = False
        self._started_at = None

        if getattr(self, "_owns_ws_streamer", True):
            await self.ws_streamer.stop()

        for sym, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._config_sigs.clear()

        if self._exit_task:
            self._exit_task.cancel()
            try:
                await self._exit_task
            except asyncio.CancelledError:
                pass
            self._exit_task = None

        self.broadcast({"type": "bot_status", "running": False})
        self.broadcast({"type": "log", "level": "info", "message": "Bot stopped"})
        try:
            try:
                from backend.database import record_operation_log
            except ImportError:
                from database import record_operation_log
            mode_str = "paper" if self.is_paper else "live"
            mode_label = "模拟交易" if self.is_paper else "实盘交易"
            record_operation_log(
                log_type="stop",
                title="系统停止",
                message=f"交易系统已停止（{mode_label}）",
                level="warning",
                mode=mode_str,
            )
            self.broadcast({"type": "operation_log"})
        except Exception:
            pass
        logger.info("TradingBot stopped (is_paper=%s)", self.is_paper)

    # ------------------------------------------------------------------
    # Symbol management
    # ------------------------------------------------------------------
    async def _start_symbol(self, config):
        symbol = config.symbol
        if symbol not in self.strategies:
            self.strategies[symbol] = MAStrategy()
            self.confirm_counts[symbol] = 0
        self._config_sigs[symbol] = _config_signature(config)
        task = asyncio.create_task(
            self._monitor_symbol(config), name=f"monitor-{symbol}"
        )
        self._tasks[symbol] = task
        logger.info("Monitoring started for %s", symbol)

    def _load_symbol_configs(self) -> list:
        """Load enabled SymbolConfig rows from the database."""
        try:
            try:
                from backend.database import SymbolConfig
            except ImportError:
                from database import SymbolConfig
            session = self.db_session_factory()
            try:
                rows = session.query(SymbolConfig).filter_by(enabled=True).all()
                for r in rows:
                    session.expunge(r)
                return rows
            finally:
                session.close()
        except Exception as exc:
            logger.error("Failed to load symbol configs: %s", exc)
            return []

    async def reload_symbols(self):
        """Hot-reload enabled symbols without resetting untouched symbols' confirmation state."""
        if not self.running:
            return
        symbol_configs = self._load_symbol_configs()
        desired = {c.symbol: c for c in symbol_configs}

        # Stop removed or disabled symbols
        for sym in list(self._tasks.keys()):
            if sym not in desired:
                task = self._tasks.pop(sym)
                self._config_sigs.pop(sym, None)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info("Hot-reload: stopped monitoring %s", sym)

        # Start newly enabled symbols, or restart ONLY those whose config actually changed
        for sym, cfg in desired.items():
            new_sig = _config_signature(cfg)
            if sym not in self._tasks:
                await self._start_symbol(cfg)
            elif self._config_sigs.get(sym) != new_sig:
                old_task = self._tasks.pop(sym)
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass
                await self._start_symbol(cfg)
                logger.info("Hot-reload: restarted %s with updated config", sym)

        self.ws_streamer.update_symbols(list(desired.keys()))
        logger.info("Hot-reload completed: active symbols = %s", list(desired.keys()))

    # ------------------------------------------------------------------
    # Per-symbol monitoring loop
    # ------------------------------------------------------------------
    async def _monitor_symbol(self, symbol_config):
        """
        Infinite loop for one symbol:
            pull K-lines → run strategy on newly closed bar → handle signal → sleep aligned to bar close.
        """
        symbol: str = symbol_config.symbol
        timeframe: str = getattr(symbol_config, "timeframe", "1m")
        htf_timeframe: str = getattr(symbol_config, "htf_timeframe", "15m")
        confirm_candles: int = getattr(symbol_config, "confirm_candles", 2)

        strategy: MAStrategy = self.strategies[symbol]
        last_closed_bar_ts = None
        first_cycle = True

        while self.running:
            try:
                df_1m = await asyncio.to_thread(self.data_feed.get_ohlcv, symbol, timeframe, 200)
                df_htf = await asyncio.to_thread(self.data_feed.get_ohlcv, symbol, htf_timeframe, 100)
                price = await asyncio.to_thread(self.data_feed.get_ticker, symbol)

                if df_1m is None or df_1m.empty or len(df_1m) < 3:
                    logger.warning("No data for %s, skipping cycle", symbol)
                    await asyncio.sleep(_sleep_until_next_poll(timeframe))
                    continue

                if price is not None:
                    self.broadcast({"type": "ticker", "symbol": symbol, "price": price})

                df_1m_ma = strategy.calculate_ma(df_1m)
                last = df_1m_ma.iloc[-1]
                ma7 = round(float(last["ma7"]), 4)
                ma25 = round(float(last["ma25"]), 4)
                ma60 = round(float(last["ma60"]), 4)
                close = round(float(last["close"]), 4)

                direction_str = strategy.check_alignment(df_1m_ma)
                direction_label = (
                    "long_aligned" if direction_str == "long"
                    else "short_aligned" if direction_str == "short"
                    else "neutral"
                )
                self.broadcast({
                    "type": "ma_update",
                    "symbol": symbol,
                    "ma7": ma7,
                    "ma25": ma25,
                    "ma60": ma60,
                    "close": close,
                    "direction": direction_label,
                })

                # Evaluate strictly on newly closed bars (drop the forming last bar on BOTH LTF and HTF!)
                closed_df = df_1m_ma.iloc[:-1].copy()
                closed_htf = df_htf.iloc[:-1].copy() if (df_htf is not None and len(df_htf) >= 2) else df_htf
                closed_candle_ts = closed_df.iloc[-1]["timestamp"]

                if first_cycle:
                    last_closed_bar_ts = closed_candle_ts
                    first_cycle = False
                elif last_closed_bar_ts != closed_candle_ts:
                    last_closed_bar_ts = closed_candle_ts
                    signal = strategy.get_signal(closed_df, closed_htf, confirm_candles)

                    if signal is None:
                        if self.confirm_counts.get(symbol, 0) != 0:
                            self.confirm_counts[symbol] = 0
                            self.broadcast({"type": "signal_reset", "symbol": symbol})
                    elif signal.get("pending"):
                        self.confirm_counts[symbol] = signal["confirm"]
                        self.broadcast({
                            "type": "signal",
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "confirm": signal["confirm"],
                            "max_confirm": signal["max_confirm"],
                            "blocked_by_wick": bool(signal.get("blocked_by_wick")),
                        })
                    else:
                        self.confirm_counts[symbol] = signal["confirm"]
                        self.broadcast({
                            "type": "signal",
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "confirm": signal["confirm"],
                            "max_confirm": signal["max_confirm"],
                            "blocked_by_wick": bool(signal.get("blocked_by_wick")),
                        })
                        if symbol not in self.positions:
                            await self._handle_signal(symbol, signal, closed_df, symbol_config)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Error in _monitor_symbol(%s): %s", symbol, exc, exc_info=True)
                self.broadcast({
                    "type": "log",
                    "level": "error",
                    "message": f"[{symbol}] monitor error: {exc}",
                })

            await asyncio.sleep(_sleep_until_next_poll(timeframe))

    def _is_daily_loss_exceeded(self, session, is_paper: bool, base_balance: float) -> bool:
        """Return True if today's realized loss exceeds max_daily_loss % of base_balance."""
        try:
            try:
                from backend.database import Trade, SettingModel
            except ImportError:
                from database import Trade, SettingModel

            row = session.query(SettingModel).filter(SettingModel.key == "max_daily_loss").first()
            max_pct = float(row.value) if row and row.value else 5.0
            if max_pct <= 0:
                return False
            if max_pct <= 0.5:
                max_pct *= 100.0

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            closed_today = (
                session.query(Trade)
                .filter(
                    Trade.status == "closed",
                    Trade.is_paper == is_paper,
                    Trade.close_time >= today_start,
                )
                .all()
            )
            today_pnl = sum(t.pnl or 0.0 for t in closed_today)
            max_loss_amount = base_balance * (max_pct / 100.0)
            if today_pnl <= -max_loss_amount:
                logger.warning(
                    "Daily loss limit reached: today_pnl=%.2f <= -%.2f (%.1f%% of %.2f)",
                    today_pnl, max_loss_amount, max_pct, base_balance,
                )
                return True
        except Exception as exc:
            logger.warning("Daily loss check error: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Signal handling → open trade
    # ------------------------------------------------------------------
    async def _handle_signal(self, symbol: str, signal: dict, df, config):
        """Open a trade when a confirmed signal arrives."""
        direction = signal["direction"]
        last = df.iloc[-1]
        ticker_price = await asyncio.to_thread(self.data_feed.get_ticker, symbol)
        entry_price = float(ticker_price) if ticker_price and ticker_price > 0 else float(last["close"])
        ma7 = float(last["ma7"])
        ma25 = float(last["ma25"])
        ma60 = float(last["ma60"])

        leverage = getattr(config, "leverage", 5)
        risk_pct = getattr(config, "risk_per_trade", 0.01)
        rr = getattr(config, "rr_ratio", 1.5)
        trailing_activate_r = float(
            getattr(config, "trailing_activate_r")
            if getattr(config, "trailing_activate_r", None) is not None
            else TRAILING_ACTIVATE_R
        )
        breakeven_buffer = float(
            getattr(config, "breakeven_buffer")
            if getattr(config, "breakeven_buffer", None) is not None
            else BREAKEVEN_BUFFER
        )

        stop_loss, take_profit, sl_distance = MAStrategy.compute_sl_tp(
            direction=direction,
            entry_price=entry_price,
            ma60=ma60,
            candle_low=float(last["low"]),
            candle_high=float(last["high"]),
            rr_ratio=rr,
        )
        if sl_distance <= 0:
            return

        is_paper = self.is_paper

        if is_paper:
            balance = 10_000.0
        else:
            balance = await asyncio.to_thread(self.data_feed.get_account_balance)
            if balance is None or balance <= 0:
                msg = f"[{symbol}] 实盘余额获取失败或为 0，已中止开仓"
                logger.error(msg)
                self.broadcast({"type": "log", "level": "error", "message": msg})
                return

        quantity = MAStrategy.calc_position_size(
            capital=balance,
            risk_pct=risk_pct,
            entry_price=entry_price,
            sl_distance=sl_distance,
            leverage=leverage,
        )
        if quantity <= 0:
            return

        try:
            try:
                from backend.database import Trade
            except ImportError:
                from database import Trade
            session = self.db_session_factory()
            try:
                if self._is_daily_loss_exceeded(session, is_paper, balance):
                    msg = f"🛑 [{symbol}] 已触发每日最大亏损风控阈值，暂停开仓"
                    self.broadcast({"type": "log", "level": "warning", "message": msg})
                    return

                exchange_order_id = None
                sl_order_id = None
                entry_fee = 0.0
                if self.executor is not None:
                    result = await asyncio.to_thread(
                        self.executor.open_trade,
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        quantity=quantity,
                        leverage=leverage,
                        is_paper=is_paper,
                    )
                    if isinstance(result, dict):
                        is_paper = bool(result.get("is_paper", is_paper))
                        entry_price = float(result.get("entry_price") or entry_price)
                        # Sync exchange-precision quantity and stop_loss back to DB (fixes T3)
                        if result.get("quantity") and float(result["quantity"]) > 0:
                            quantity = float(result["quantity"])
                        if result.get("stop_loss") and float(result["stop_loss"]) > 0:
                            stop_loss = float(result["stop_loss"])
                        exchange_order_id = result.get("exchange_order_id")
                        sl_order_id = result.get("sl_order_id")
                        entry_fee = float(result.get("entry_fee") or 0.0)

                        # Recompute actual 1R distance & TP from actual fill price and formatted SL
                        actual_r = abs(entry_price - stop_loss)
                        if actual_r > 0:
                            sl_distance = actual_r
                            take_profit = (
                                entry_price + sl_distance * rr
                                if direction == "long"
                                else entry_price - sl_distance * rr
                            )

                trade = Trade(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    quantity=quantity,
                    status="open",
                    is_paper=is_paper,
                    open_time=datetime.now(timezone.utc),
                    timeframe=getattr(config, "timeframe", "1m"),
                    confirm_count=signal["confirm"],
                    ma7_at_entry=ma7,
                    ma25_at_entry=ma25,
                    ma60_at_entry=ma60,
                    exchange_order_id=exchange_order_id,
                    sl_order_id=sl_order_id,
                    trailing_active=False,
                    initial_risk=sl_distance,
                    trailing_activate_r=trailing_activate_r,
                    breakeven_buffer=breakeven_buffer,
                )
                session.add(trade)
                session.commit()
                session.refresh(trade)

                self.positions[symbol] = {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "entry_price": trade.entry_price,
                    "stop_loss": trade.stop_loss,
                    "take_profit": trade.take_profit,
                    "quantity": trade.quantity,
                    "status": "open",
                    "is_paper": trade.is_paper,
                    "open_time": trade.open_time,
                    "timeframe": getattr(config, "timeframe", "1m"),
                    "exchange_order_id": exchange_order_id,
                    "sl_order_id": sl_order_id,
                    "trailing_active": False,
                    "risk_r": sl_distance,
                    "trailing_activate_r": trailing_activate_r,
                    "breakeven_buffer": breakeven_buffer,
                    "entry_fee": entry_fee,
                }

                trade_dict = {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "entry_price": trade.entry_price,
                    "stop_loss": trade.stop_loss,
                    "take_profit": trade.take_profit,
                    "quantity": trade.quantity,
                    "is_paper": trade.is_paper,
                    "open_time": trade.open_time.isoformat(),
                }
                self.broadcast({"type": "trade_opened", "trade": trade_dict})

                msg = (
                    f"🟢 {symbol} {'LONG' if direction == 'long' else 'SHORT'} opened\n"
                    f"Entry: {entry_price}  SL: {stop_loss:.4f}  TP: {take_profit:.4f}\n"
                    f"Qty: {quantity}  {'[PAPER]' if is_paper else '[LIVE]'}"
                )
                try:
                    try:
                        from backend.database import record_operation_log
                    except ImportError:
                        from database import record_operation_log
                    dir_label = "做多" if direction == "long" else "做空"
                    record_operation_log(
                        log_type="open",
                        title=f"{symbol} {dir_label}开仓",
                        message=f"入场价: {entry_price} | 止损: {stop_loss:.4f} | 止盈: {take_profit:.4f} | 数量: {quantity}",
                        level="info",
                        symbol=symbol,
                        mode="paper" if is_paper else "live",
                        db=session,
                    )
                    self.broadcast({"type": "operation_log"})
                except Exception:
                    pass
                self.broadcast({"type": "log", "level": "info", "message": msg})
                if self.notifier:
                    try:
                        await self.notifier.send(msg, parse_mode=None)
                    except Exception:
                        pass

            finally:
                session.close()

        except Exception as exc:
            logger.error("_handle_signal(%s) failed: %s", symbol, exc, exc_info=True)
            self.broadcast({
                "type": "log",
                "level": "error",
                "message": f"[{symbol}] open trade error: {exc}",
            })
            if self.notifier and not is_paper:
                try:
                    await self.notifier.notify_error(f"[{symbol}] 实盘开仓失败: {exc}")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Exit monitoring + live exchange position reconciliation
    # ------------------------------------------------------------------
    async def _exit_monitor(self):
        """Background task that checks open positions every 15 seconds."""
        while self.running:
            try:
                self._exit_cycle_count += 1
                await self._check_exits()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("_exit_monitor error: %s", exc, exc_info=True)
            await asyncio.sleep(15)

    async def _sync_exchange_sl(self, symbol: str, pos: dict, new_sl: float) -> float:
        """
        In live mode, move the resting exchange STOP_MARKET order to match *new_sl*
        and update pos['sl_order_id']. Returns the resulting stop_loss price.
        """
        if pos.get("is_paper", True) or self.executor is None:
            return new_sl
        if not hasattr(self.executor, "update_stop_loss"):
            return new_sl
        res = await asyncio.to_thread(
            self.executor.update_stop_loss,
            symbol,
            pos["direction"],
            new_sl,
            pos["quantity"],
            pos.get("sl_order_id"),
        )
        if isinstance(res, dict) and res.get("updated"):
            pos["sl_order_id"] = res.get("sl_order_id")
            return float(res.get("stop_loss") or new_sl)
        return new_sl

    async def _reconcile_live_position(self, symbol: str, pos: dict, fallback_price: Optional[float]) -> bool:
        """
        Verify a live position still exists on the exchange.
        If the exchange already closed it (e.g. SL filled), reconcile DB & memory
        and return True so the caller skips further checks.
        """
        if pos.get("is_paper", True) or self.executor is None or not hasattr(self.executor, "get_position"):
            return False
        ex_pos = await asyncio.to_thread(self.executor.get_position, symbol)
        if ex_pos is None or ex_pos["size"] > 0:
            return False

        # Exchange reports 0 position — check if our SL order was filled to get exact fill price
        fill_price = fallback_price or pos["stop_loss"]
        close_reason = "exchange_closed"
        sl_order_id = pos.get("sl_order_id")
        if sl_order_id and hasattr(self.executor, "fetch_order_fill"):
            sl_order = await asyncio.to_thread(self.executor.fetch_order_fill, symbol, sl_order_id)
            if isinstance(sl_order, dict):
                status_s = str(sl_order.get("status") or "").lower()
                avg = float(sl_order.get("average") or sl_order.get("price") or 0.0)
                if avg > 0:
                    fill_price = avg
                if status_s == "closed":
                    if pos.get("trailing_active"):
                        close_reason = "trailing_stop"
                    elif (pos["direction"] == "long" and pos["stop_loss"] > pos["entry_price"]) or (
                        pos["direction"] == "short" and pos["stop_loss"] < pos["entry_price"]
                    ):
                        close_reason = "profit_lock_stop"
                    else:
                        close_reason = "stop_loss"

        logger.info(
            "[LIVE] Reconciled %s: exchange position is 0; marking trade #%s closed (%s @ %s)",
            symbol, pos.get("id"), close_reason, fill_price,
        )
        await self._close_trade(symbol, pos, close_reason, float(fill_price), skip_exchange_order=True)
        return True

    async def _check_exits(self):
        """Check all open positions for exit conditions and keep live exchange SL synced."""
        for symbol, pos in list(self.positions.items()):
            try:
                direction = pos["direction"]
                entry_price = float(pos["entry_price"])
                stop_loss = float(pos["stop_loss"])
                take_profit = float(pos["take_profit"])
                open_time = pos["open_time"]
                timeframe = pos.get("timeframe", "1m")
                trailing_active = bool(pos.get("trailing_active", False))
                risk_r = float(pos.get("risk_r") or 0.0)
                if risk_r <= 0:
                    risk_r = abs(entry_price - stop_loss) if (
                        (direction == "long" and stop_loss < entry_price)
                        or (direction == "short" and stop_loss > entry_price)
                    ) else abs(take_profit - entry_price) / 1.5
                    pos["risk_r"] = risk_r

                price = await asyncio.to_thread(self.data_feed.get_ticker, symbol)

                # Periodically reconcile live position with exchange (fixes T5)
                if not pos.get("is_paper", True) and (self._exit_cycle_count % 2 == 1):
                    if await self._reconcile_live_position(symbol, pos, price):
                        continue

                close_reason = None
                sl_updated = False
                prev_sl = stop_loss

                if price is not None and risk_r > 0:
                    pos_act_r = float(
                        pos["trailing_activate_r"]
                        if pos.get("trailing_activate_r") is not None
                        else TRAILING_ACTIVATE_R
                    )
                    pos_be_buf = float(
                        pos["breakeven_buffer"]
                        if pos.get("breakeven_buffer") is not None
                        else BREAKEVEN_BUFFER
                    )
                    if direction == "long":
                        if price <= stop_loss:
                            close_reason = "trailing_stop" if trailing_active else "stop_loss"
                        elif not trailing_active and price >= entry_price + pos_act_r * risk_r:
                            trailing_active = True
                            stop_loss = MAStrategy.compute_trailing_lock_sl(
                                "long", entry_price, risk_r, stop_loss,
                                activate_r=pos_act_r, breakeven_buffer=pos_be_buf,
                            )
                            sl_updated = True
                            pos["trailing_active"] = True
                            self.broadcast({
                                "type": "log",
                                "level": "info",
                                "message": f"🔒 [{symbol}] 浮盈达 {pos_act_r:.2f}R，止损移至保本+{pos_be_buf*100:.2f}% ({stop_loss:.4f}) 并开启 MA25 移动止盈",
                            })
                    else:  # short
                        if price >= stop_loss:
                            close_reason = "trailing_stop" if trailing_active else "stop_loss"
                        elif not trailing_active and price <= entry_price - pos_act_r * risk_r:
                            trailing_active = True
                            stop_loss = MAStrategy.compute_trailing_lock_sl(
                                "short", entry_price, risk_r, stop_loss,
                                activate_r=pos_act_r, breakeven_buffer=pos_be_buf,
                            )
                            sl_updated = True
                            pos["trailing_active"] = True
                            self.broadcast({
                                "type": "log",
                                "level": "info",
                                "message": f"🔒 [{symbol}] 浮盈达 {pos_act_r:.2f}R，止损移至保本-{pos_be_buf*100:.2f}% ({stop_loss:.4f}) 并开启 MA25 移动止盈",
                            })

                if close_reason:
                    await self._close_trade(symbol, pos, close_reason, price)
                    continue

                # 2. Only once MA25 trailing is active (>= 1.2R): tighten SL along MA25 & exit if bar closes beyond MA25
                strategy = self.strategies.get(symbol)
                if trailing_active and strategy is not None:
                    df = await asyncio.to_thread(self.data_feed.get_ohlcv, symbol, timeframe, 100)
                    if df is not None and not df.empty:
                        df = strategy.calculate_ma(df)
                        eval_df = df.iloc[:-1] if len(df) >= 2 else df

                        if len(eval_df) >= 1:
                            ma25_val = float(eval_df.iloc[-1]["ma25"])
                            if direction == "long":
                                new_sl = max(stop_loss, ma25_val * 0.9995)
                                if new_sl > stop_loss:
                                    stop_loss = new_sl
                                    sl_updated = True
                            else:
                                new_sl = min(stop_loss, ma25_val * 1.0005)
                                if new_sl < stop_loss:
                                    stop_loss = new_sl
                                    sl_updated = True

                        position_dict = {
                            "direction": direction,
                            "open_time": open_time,
                            "trailing_active": True,
                        }
                        exit_sig = strategy.check_exit(eval_df, position_dict)
                        if exit_sig:
                            await self._close_trade(
                                symbol, pos, exit_sig, price or float(df.iloc[-1]["close"])
                            )
                            continue

                # 3. Persist updated SL & sync exchange SL order (only if SL moved meaningfully >= 0.02% or trailing toggled)
                if sl_updated:
                    moved_enough = (prev_sl <= 0) or (abs(stop_loss - prev_sl) / prev_sl >= 0.0002)
                    if moved_enough and not pos.get("is_paper", True):
                        stop_loss = await self._sync_exchange_sl(symbol, pos, stop_loss)
                    pos["stop_loss"] = stop_loss
                    self._persist_trailing_update(
                        pos["id"], stop_loss, trailing_active, pos.get("sl_order_id")
                    )

            except Exception as exc:
                logger.error("_check_exits(%s) error: %s", symbol, exc, exc_info=True)

    def _persist_trailing_update(
        self,
        trade_id: int,
        stop_loss: float,
        trailing_active: bool,
        sl_order_id: Optional[str] = None,
    ):
        """Persist updated trailing stop_loss / trailing_active / sl_order_id to SQLite."""
        try:
            try:
                from backend.database import Trade
            except ImportError:
                from database import Trade
            session = self.db_session_factory()
            try:
                updates = {
                    "stop_loss": stop_loss,
                    "trailing_active": trailing_active,
                }
                if sl_order_id is not None:
                    updates["sl_order_id"] = sl_order_id
                session.query(Trade).filter_by(id=trade_id, status="open").update(updates)
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            logger.warning("Failed to persist trailing SL update for trade %s: %s", trade_id, exc)

    async def _close_trade(
        self,
        symbol: str,
        pos,
        reason: str,
        exit_price: float,
        skip_exchange_order: bool = False,
    ):
        """Execute live close if needed, atomically persist closure, and broadcast."""
        trade_id = pos["id"] if isinstance(pos, dict) else getattr(pos, "id", None)
        is_paper = pos.get("is_paper", True) if isinstance(pos, dict) else getattr(pos, "is_paper", True)
        total_fee = float(pos.get("entry_fee") or 0.0) if isinstance(pos, dict) else 0.0

        try:
            if not is_paper and not skip_exchange_order and self.executor is not None:
                close_res = await asyncio.to_thread(
                    self.executor.close_trade, pos, exit_price, reason
                )
                if isinstance(close_res, dict):
                    if close_res.get("exit_price"):
                        exit_price = float(close_res["exit_price"])
                    if close_res.get("close_reason"):
                        reason = str(close_res["close_reason"])
                    if close_res.get("fee") is not None:
                        total_fee = float(close_res["fee"])

            trade_dict = None
            pnl = 0.0
            pnl_pct = 0.0

            session = self.db_session_factory()
            try:
                try:
                    from backend.database import Trade
                except ImportError:
                    from database import Trade
                db_trade = session.query(Trade).filter_by(id=trade_id).first()
                if db_trade is None:
                    logger.warning("Trade id=%s not found in DB when closing", trade_id)
                    self.positions.pop(symbol, None)
                    return

                # Guard against double-close race with manual_close_trade (fixes T9)
                if db_trade.status != "open":
                    logger.info("Trade id=%s already closed (status=%s); skipping overwrite.", trade_id, db_trade.status)
                    self.positions.pop(symbol, None)
                    return

                db_trade.exit_price = exit_price
                db_trade.close_reason = reason
                db_trade.close_time = datetime.now(timezone.utc)
                db_trade.status = "closed"

                if db_trade.direction == "long":
                    gross_pnl = (exit_price - db_trade.entry_price) * db_trade.quantity
                else:
                    gross_pnl = (db_trade.entry_price - exit_price) * db_trade.quantity

                db_trade.pnl = round(gross_pnl - total_fee, 4)
                notional = db_trade.entry_price * db_trade.quantity
                db_trade.pnl_pct = round((db_trade.pnl / notional * 100.0) if notional > 0 else 0.0, 4)

                pnl = db_trade.pnl
                pnl_pct = db_trade.pnl_pct
                trade_dict = {
                    "id": db_trade.id,
                    "symbol": db_trade.symbol,
                    "direction": db_trade.direction,
                    "entry_price": db_trade.entry_price,
                    "exit_price": db_trade.exit_price,
                    "pnl": db_trade.pnl,
                    "pnl_pct": db_trade.pnl_pct,
                    "close_reason": reason,
                    "close_time": db_trade.close_time.isoformat() if db_trade.close_time else None,
                }
                session.commit()
            finally:
                session.close()

            self.positions.pop(symbol, None)

            if trade_dict:
                self.broadcast({"type": "trade_closed", "trade": trade_dict})

            emoji = "✅" if pnl >= 0 else "❌"
            pnl_sign = "+" if pnl >= 0 else ""
            mode_tag = "PAPER" if is_paper else "LIVE"
            entry_p = trade_dict["entry_price"] if trade_dict else ""
            msg = (
                f"{emoji} {symbol} closed ({reason}) [{mode_tag}]\n"
                f"Entry: {entry_p}  Exit: {exit_price}\n"
                f"PnL: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)"
            )
            try:
                try:
                    from backend.database import record_operation_log
                except ImportError:
                    from database import record_operation_log
                record_operation_log(
                    log_type="close",
                    title=f"{symbol} 平仓 ({reason})",
                    message=f"入场: {entry_p} | 出场: {exit_price} | 盈亏: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)",
                    level="success" if pnl >= 0 else "warning",
                    symbol=symbol,
                    mode="paper" if is_paper else "live",
                )
                self.broadcast({"type": "operation_log"})
            except Exception:
                pass
            self.broadcast({"type": "log", "level": "info", "message": msg})
            if self.notifier:
                try:
                    await self.notifier.send(msg, parse_mode=None)
                except Exception as exc:
                    logger.warning("Telegram close notification failed: %s", exc)

        except Exception as exc:
            logger.error("_close_trade(%s) failed: %s", symbol, exc, exc_info=True)
            if self.notifier and not is_paper:
                try:
                    await self.notifier.notify_error(f"[{symbol}] 实盘平仓失败 ({reason}): {exc}")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # WebSocket broadcasting & Status
    # ------------------------------------------------------------------
    def register_ws_callback(self, callback: Callable[[dict], None]):
        if callback not in self.ws_callbacks:
            self.ws_callbacks.append(callback)

    def broadcast(self, message: dict):
        self._persist_system_log_event(message)
        for cb in list(self.ws_callbacks):
            try:
                cb(message)
            except Exception as exc:
                logger.warning("ws_callback error: %s", exc)

    def _persist_system_log_event(self, message: dict) -> None:
        """Persist homepage-style system log events (signals, logs, trade notices) into SQLite."""
        if not isinstance(message, dict):
            return
        mtype = message.get("type")
        if mtype not in {"signal", "log", "trade_opened", "trade_closed"}:
            return
        try:
            try:
                from database import record_system_log
            except ImportError:
                from backend.database import record_system_log

            log_msg = None
            log_level = "info"
            log_symbol = None

            if mtype == "signal":
                log_symbol = message.get("symbol")
                direction = str(message.get("direction") or "").upper()
                confirm = message.get("confirm", 0)
                max_c = message.get("max_confirm", 2)
                suffix = "（反向长影线拦截，暂缓开仓）" if message.get("blocked_by_wick") else ""
                log_msg = f"[信号] {log_symbol} {direction} 确认 {confirm}/{max_c}{suffix}"
                log_level = "info"
            elif mtype == "log":
                text_msg = str(message.get("message") or "").strip()
                if text_msg:
                    log_msg = text_msg
                    log_level = str(message.get("level") or "info")
                    log_symbol = message.get("symbol")
            elif mtype == "trade_opened":
                t = message.get("trade") or {}
                log_symbol = t.get("symbol")
                d = str(t.get("direction") or "").upper()
                log_msg = f"[开仓通知] {d} {log_symbol} 价格:{t.get('entry_price')} 止损:{t.get('stop_loss')} 止盈:{t.get('take_profit')}"
                log_level = "success"
            elif mtype == "trade_closed":
                t = message.get("trade") or {}
                log_symbol = t.get("symbol")
                pnl = t.get("pnl")
                pnl_str = f"{'+' if pnl >= 0 else ''}${pnl:.2f}" if isinstance(pnl, (int, float)) else ""
                log_level = "success" if (isinstance(pnl, (int, float)) and pnl >= 0) else "warning"
                log_msg = f"[平仓通知] {log_symbol} ({t.get('close_reason') or '平仓'}) 盈亏:{pnl_str}"

            if log_msg:
                session = self.db_session_factory() if callable(self.db_session_factory) else None
                try:
                    record_system_log(log_msg, level=log_level, symbol=log_symbol, db=session)
                finally:
                    if session is not None:
                        session.close()
        except Exception as exc:
            logger.debug("Failed to persist system log event: %s", exc)

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "symbols": {
                sym: {
                    "has_position": sym in self.positions,
                    "confirm_count": self.confirm_counts.get(sym, 0),
                    "strategy_direction": (
                        self.strategies[sym]._last_direction
                        if sym in self.strategies
                        else None
                    ),
                }
                for sym in self.strategies
            },
        }
