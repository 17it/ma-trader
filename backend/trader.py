"""
TradingBot — multi-symbol async monitoring loop.

Each symbol runs in its own asyncio.Task.  Strategy signals trigger trade
execution (real or paper) via the injected *executor* and notifications via
*notifier*.  WebSocket clients receive real-time updates through registered
callbacks.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

try:
    from backend.strategy import MAStrategy
    from backend.binance_ws import BinanceWSStreamer
except ImportError:
    from strategy import MAStrategy
    from binance_ws import BinanceWSStreamer

logger = logging.getLogger(__name__)

# Polling interval per timeframe (seconds)
_TF_INTERVAL: Dict[str, int] = {
    "1m": 15,
    "3m": 30,
    "5m": 45,
    "15m": 60,
    "30m": 120,
    "1h": 300,
}
_DEFAULT_INTERVAL = 60


def _interval_for(timeframe: str) -> int:
    return _TF_INTERVAL.get(timeframe, _DEFAULT_INTERVAL)


class TradingBot:
    """
    Manages concurrent monitoring of multiple trading symbols.

    Parameters
    ----------
    db_session_factory:
        Callable that returns a SQLAlchemy Session (e.g. ``SessionLocal``).
    data_feed:
        A ``DataFeed`` instance used to pull OHLCV and ticker data.
    executor:
        Object with ``open_trade(...)`` / ``close_trade(...)`` methods that
        interact with the exchange (or paper-trade).
    notifier:
        Object with ``send(message: str)`` for Telegram / other alerts.
    """

    def __init__(self, db_session_factory, data_feed, executor, notifier):
        self.db_session_factory = db_session_factory
        self.data_feed = data_feed
        self.executor = executor
        self.notifier = notifier

        self.running: bool = False
        self.strategies: Dict[str, MAStrategy] = {}   # symbol → MAStrategy
        self.positions: Dict[str, dict] = {}          # symbol → pos_info dict (avoids detached session bugs)
        self.confirm_counts: Dict[str, int] = {}       # mirrored for quick access
        self.ws_callbacks: List[Callable[[dict], None]] = []

        self._tasks: Dict[str, asyncio.Task] = {}
        self._exit_task: Optional[asyncio.Task] = None

        # Real-time WebSocket streamer for zero-latency, zero-HTTP prices
        self.ws_streamer = BinanceWSStreamer()
        self.ws_streamer.register_callback(lambda msg: self.broadcast(msg))
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
        self.broadcast({"type": "bot_status", "running": True})
        self.broadcast({"type": "log", "level": "info", "message": "Bot started"})

        symbol_configs = self._load_symbol_configs()

        # Start real-time Binance WebSocket stream for all active symbols
        active_symbols = [c.symbol for c in symbol_configs if getattr(c, "symbol", None)]
        self.ws_streamer.start(active_symbols)

        # Restore open trades from DB into memory
        try:
            try:
                from backend.database import Trade
            except ImportError:
                from database import Trade
            session = self.db_session_factory()
            try:
                open_trades = session.query(Trade).filter_by(status="open").all()
                for ot in open_trades:
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
        logger.info("TradingBot started with %d symbols", len(symbol_configs))

    async def stop(self):
        """Cancel all monitoring tasks and mark bot as stopped."""
        self.running = False

        await self.ws_streamer.stop()

        for sym, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        if self._exit_task:
            self._exit_task.cancel()
            try:
                await self._exit_task
            except asyncio.CancelledError:
                pass
            self._exit_task = None

        self.broadcast({"type": "bot_status", "running": False})
        self.broadcast({"type": "log", "level": "info", "message": "Bot stopped"})
        logger.info("TradingBot stopped")

    # ------------------------------------------------------------------
    # Symbol management
    # ------------------------------------------------------------------
    async def _start_symbol(self, config):
        symbol = config.symbol
        if symbol not in self.strategies:
            self.strategies[symbol] = MAStrategy()
            self.confirm_counts[symbol] = 0
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
                return session.query(SymbolConfig).filter_by(enabled=True).all()
            finally:
                session.close()
        except Exception as exc:
            logger.error("Failed to load symbol configs: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Per-symbol monitoring loop
    # ------------------------------------------------------------------
    async def _monitor_symbol(self, symbol_config):
        """
        Infinite loop for one symbol:
            pull K-lines → run strategy → handle signal → sleep.
        """
        symbol: str = symbol_config.symbol
        timeframe: str = getattr(symbol_config, "timeframe", "1m")
        htf_timeframe: str = getattr(symbol_config, "htf_timeframe", "15m")
        confirm_candles: int = getattr(symbol_config, "confirm_candles", 2)
        interval: int = _interval_for(timeframe)

        strategy: MAStrategy = self.strategies[symbol]
        last_closed_bar_ts = None
        first_cycle = True

        while self.running:
            try:
                # --- Fetch data ---
                df_1m = self.data_feed.get_ohlcv(symbol, timeframe, limit=200)
                df_htf = self.data_feed.get_ohlcv(symbol, htf_timeframe, limit=100)
                price = self.data_feed.get_ticker(symbol)

                if df_1m is None or df_1m.empty or len(df_1m) < 3:
                    logger.warning("No data for %s, skipping cycle", symbol)
                    await asyncio.sleep(interval)
                    continue

                # --- Broadcast ticker ---
                if price is not None:
                    self.broadcast({"type": "ticker", "symbol": symbol, "price": price})

                # --- Compute MAs and broadcast real-time UI update ---
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

                # --- Strategy confirmation: Strictly evaluate on newly closed candles (Bar Close) ---
                # In df_1m: the last row iloc[-1] is the unclosed forming bar; iloc[:-1] are fully closed bars.
                closed_df = df_1m.iloc[:-1].copy()
                closed_candle_ts = closed_df.iloc[-1]["timestamp"]

                if first_cycle:
                    # On startup, record the current closed candle timestamp so we only count newly closed candles onwards
                    last_closed_bar_ts = closed_candle_ts
                    first_cycle = False
                elif last_closed_bar_ts != closed_candle_ts:
                    # A new 1-minute candle has officially closed!
                    last_closed_bar_ts = closed_candle_ts
                    signal = strategy.get_signal(closed_df, df_htf, confirm_candles)

                    if signal is None:
                        # Reset
                        if self.confirm_counts.get(symbol, 0) != 0:
                            self.confirm_counts[symbol] = 0
                            self.broadcast({"type": "signal_reset", "symbol": symbol})
                    elif signal.get("pending"):
                        # Partial confirmation on closed candle — broadcast progress
                        self.confirm_counts[symbol] = signal["confirm"]
                        self.broadcast({
                            "type": "signal",
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "confirm": signal["confirm"],
                            "max_confirm": signal["max_confirm"],
                        })
                    else:
                        # Full confirmation reached on closed candles!
                        self.confirm_counts[symbol] = signal["confirm"]
                        self.broadcast({
                            "type": "signal",
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "confirm": signal["confirm"],
                            "max_confirm": signal["max_confirm"],
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

            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Signal handling → open trade
    # ------------------------------------------------------------------
    async def _handle_signal(self, symbol: str, signal: dict, df, config):
        """Open a trade when a confirmed signal arrives."""
        direction = signal["direction"]
        last = df.iloc[-1]
        ticker_price = self.data_feed.get_ticker(symbol)
        entry_price = float(ticker_price) if ticker_price and ticker_price > 0 else float(last["close"])
        ma7 = float(last["ma7"])
        ma25 = float(last["ma25"])
        ma60 = float(last["ma60"])

        leverage = getattr(config, "leverage", 5)
        risk_pct = getattr(config, "risk_per_trade", 0.01)
        rr = getattr(config, "rr_ratio", 1.5)

        # Simple SL/TP calculation
        atr_proxy = float(df["high"].iloc[-20:].max() - df["low"].iloc[-20:].min()) / 20
        sl_distance = max(atr_proxy, entry_price * 0.002)
        tp_distance = sl_distance * rr

        if direction == "long":
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        # Balance-based position sizing
        balance = self.data_feed.get_account_balance() or 1000.0
        risk_amount = balance * risk_pct
        quantity = round((risk_amount * leverage) / sl_distance, 6)

        try:
            try:
                from backend.database import Trade
            except ImportError:
                from database import Trade
            session = self.db_session_factory()
            try:
                is_paper = True  # default; executor can override
                if self.executor is not None:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.executor.open_trade(
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            quantity=quantity,
                            leverage=leverage,
                        ),
                    )
                    is_paper = getattr(result, "is_paper", True)

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
                )
                session.add(trade)
                session.commit()
                session.refresh(trade)
                
                # Store plain dictionary in self.positions to avoid detached instance errors
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
                self.broadcast({"type": "log", "level": "info", "message": msg})
                if self.notifier:
                    try:
                        await self.notifier.send(msg)
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

    # ------------------------------------------------------------------
    # Exit monitoring
    # ------------------------------------------------------------------
    async def _exit_monitor(self):
        """Background task that checks open positions every 15 seconds."""
        while self.running:
            try:
                await self._check_exits()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("_exit_monitor error: %s", exc, exc_info=True)
            await asyncio.sleep(15)

    async def _check_exits(self):
        """Check all open positions for exit conditions."""
        for symbol, pos in list(self.positions.items()):
            try:
                direction = pos["direction"] if isinstance(pos, dict) else pos.direction
                stop_loss = pos["stop_loss"] if isinstance(pos, dict) else pos.stop_loss
                take_profit = pos["take_profit"] if isinstance(pos, dict) else pos.take_profit
                open_time = pos["open_time"] if isinstance(pos, dict) else pos.open_time
                timeframe = pos.get("timeframe", "1m") if isinstance(pos, dict) else getattr(pos, "timeframe", "1m")

                # 1. First check Stop Loss / Take Profit via zero-HTTP ticker cache
                price = self.data_feed.get_ticker(symbol)
                close_reason = None
                if price is not None:
                    if direction == "long":
                        if price <= stop_loss:
                            close_reason = "stop_loss"
                        elif price >= take_profit:
                            close_reason = "take_profit"
                    else:
                        if price >= stop_loss:
                            close_reason = "stop_loss"
                        elif price <= take_profit:
                            close_reason = "take_profit"

                if close_reason:
                    await self._close_trade(symbol, pos, close_reason, price)
                    continue

                # 2. Check MA indicator exit only if SL/TP not hit
                strategy = self.strategies.get(symbol)
                if strategy is not None:
                    df = self.data_feed.get_ohlcv(symbol, timeframe, limit=100)
                    if df is not None and not df.empty:
                        df = strategy.calculate_ma(df)
                        position_dict = {
                            "direction": direction,
                            "open_time": open_time,
                        }
                        exit_sig = strategy.check_exit(df, position_dict)
                        if exit_sig:
                            await self._close_trade(
                                symbol, pos, exit_sig, price or float(df.iloc[-1]["close"])
                            )

            except Exception as exc:
                logger.error("_check_exits(%s) error: %s", symbol, exc, exc_info=True)

    async def _close_trade(self, symbol: str, pos, reason: str, exit_price: float):
        """Persist trade closure and broadcast the event without detached instance errors."""
        trade_id = pos["id"] if isinstance(pos, dict) else getattr(pos, "id", None)
        try:
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

                db_trade.exit_price = exit_price
                db_trade.close_reason = reason
                db_trade.close_time = datetime.now(timezone.utc)
                db_trade.status = "closed"

                # PnL
                if db_trade.direction == "long":
                    db_trade.pnl = (exit_price - db_trade.entry_price) * db_trade.quantity
                else:
                    db_trade.pnl = (db_trade.entry_price - exit_price) * db_trade.quantity
                db_trade.pnl_pct = db_trade.pnl / (db_trade.entry_price * db_trade.quantity) * 100

                # Read all values before session commit / close to prevent DetachedInstanceError
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
            msg = (
                f"{emoji} {symbol} closed ({reason})\n"
                f"Exit: {exit_price}  PnL: {trade.pnl:.2f} ({trade.pnl_pct:.1f}%)"
            )
            self.broadcast({"type": "log", "level": "info", "message": msg})
            if self.notifier:
                try:
                    await self.notifier.send(msg)
                except Exception:
                    pass

        except Exception as exc:
            logger.error("_close_trade(%s) failed: %s", symbol, exc, exc_info=True)

    # ------------------------------------------------------------------
    # WebSocket broadcasting
    # ------------------------------------------------------------------
    def register_ws_callback(self, callback: Callable[[dict], None]):
        """Register a callable that receives every broadcast message dict."""
        if callback not in self.ws_callbacks:
            self.ws_callbacks.append(callback)

    def broadcast(self, message: dict):
        """Send *message* to all registered WebSocket callbacks."""
        for cb in list(self.ws_callbacks):
            try:
                cb(message)
            except Exception as exc:
                logger.warning("ws_callback error: %s", exc)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """Return a snapshot of the bot's current state."""
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
