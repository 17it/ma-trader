"""
MA Trader - Order Execution Layer
Supports both live (ccxt/Binance) and paper trading modes.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class Executor:
    """
    Handles order placement for both live and paper trading.

    In paper mode no exchange calls are made; positions are tracked
    purely in the database.
    """

    def __init__(self, exchange, config: dict):
        """
        Args:
            exchange: ccxt exchange instance (may be None for paper-only).
            config:   dict with keys like 'leverage', 'trading_mode', etc.
        """
        self.exchange = exchange
        self.config = config

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calc_position_size(
        self,
        entry: float,
        stop_loss: float,
        balance: float,
        risk_pct: float,
    ) -> float:
        """
        Calculate quantity to trade based on fixed-fractional risk.

        Args:
            entry:     Planned entry price.
            stop_loss: Stop-loss price.
            balance:   Current account balance in quote currency.
            risk_pct:  Fraction of balance to risk (e.g. 0.01 = 1 %).

        Returns:
            Quantity in base currency (rounded to 4 decimal places).
        """
        risk_amount = balance * risk_pct
        price_risk = abs(entry - stop_loss)
        if price_risk == 0:
            return 0.0
        qty = risk_amount / price_risk
        return round(qty, 4)

    # ------------------------------------------------------------------
    # Open positions
    # ------------------------------------------------------------------

    def open_long(
        self,
        symbol: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        is_paper: bool = False,
    ) -> dict:
        """
        Open a long position.

        Returns a Trade dict (not persisted – the caller saves to DB).
        """
        trade = {
            "symbol": symbol,
            "direction": "long",
            "entry_price": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "quantity": qty,
            "status": "open",
            "is_paper": is_paper,
            "open_time": datetime.now(timezone.utc),
        }

        if is_paper:
            logger.info(f"[PAPER] Open long {symbol} qty={qty} @ {entry} SL={stop_loss} TP={take_profit}")
            return trade

        # Live trading via ccxt
        try:
            order = self.exchange.create_market_buy_order(symbol, qty)
            trade["entry_price"] = float(order.get("average") or order.get("price") or entry)
            trade["exchange_order_id"] = order.get("id")

            # Place stop-loss order
            sl_params = {"stopPrice": stop_loss, "reduceOnly": True}
            try:
                sl_order = self.exchange.create_order(
                    symbol, "stop_market", "sell", qty, None, sl_params
                )
                trade["sl_order_id"] = sl_order.get("id")
            except Exception as exc:
                logger.warning(f"Failed to place SL order: {exc}")

            logger.info(f"[LIVE] Opened long {symbol} qty={qty} @ {trade['entry_price']}")
        except Exception as exc:
            logger.error(f"open_long failed for {symbol}: {exc}")
            raise

        return trade

    def open_short(
        self,
        symbol: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        is_paper: bool = False,
    ) -> dict:
        """Open a short position."""
        trade = {
            "symbol": symbol,
            "direction": "short",
            "entry_price": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "quantity": qty,
            "status": "open",
            "is_paper": is_paper,
            "open_time": datetime.now(timezone.utc),
        }

        if is_paper:
            logger.info(f"[PAPER] Open short {symbol} qty={qty} @ {entry} SL={stop_loss} TP={take_profit}")
            return trade

        try:
            order = self.exchange.create_market_sell_order(symbol, qty)
            trade["entry_price"] = float(order.get("average") or order.get("price") or entry)
            trade["exchange_order_id"] = order.get("id")

            sl_params = {"stopPrice": stop_loss, "reduceOnly": True}
            try:
                sl_order = self.exchange.create_order(
                    symbol, "stop_market", "buy", qty, None, sl_params
                )
                trade["sl_order_id"] = sl_order.get("id")
            except Exception as exc:
                logger.warning(f"Failed to place SL order: {exc}")

            logger.info(f"[LIVE] Opened short {symbol} qty={qty} @ {trade['entry_price']}")
        except Exception as exc:
            logger.error(f"open_short failed for {symbol}: {exc}")
            raise

        return trade

    # ------------------------------------------------------------------
    # Close positions
    # ------------------------------------------------------------------

    def close_position(self, trade: dict, exit_price: float, reason: str) -> dict:
        """
        Close an open position and calculate P&L.

        Args:
            trade:      Trade dict (as stored in DB / returned by open_*).
            exit_price: Price at which the position is closed.
            reason:     "take_profit" | "stop_loss" | "manual" | "signal"

        Returns:
            Updated trade dict with exit_price, pnl, pnl_pct, close_time, close_reason.
        """
        entry = trade["entry_price"]
        qty = trade["quantity"]
        direction = trade["direction"]

        if direction == "long":
            pnl = (exit_price - entry) * qty
        else:
            pnl = (entry - exit_price) * qty

        pnl_pct = pnl / (entry * qty) * 100 if (entry * qty) != 0 else 0.0

        trade["exit_price"] = exit_price
        trade["pnl"] = round(pnl, 4)
        trade["pnl_pct"] = round(pnl_pct, 4)
        trade["close_time"] = datetime.now(timezone.utc)
        trade["close_reason"] = reason
        trade["status"] = "closed"

        if not trade.get("is_paper"):
            # Cancel any resting stop-loss order and market-close the position
            sl_order_id = trade.get("sl_order_id")
            if sl_order_id and self.exchange:
                try:
                    self.exchange.cancel_order(sl_order_id, trade["symbol"])
                except Exception as exc:
                    logger.warning(f"Failed to cancel SL order {sl_order_id}: {exc}")

            if self.exchange:
                try:
                    if direction == "long":
                        self.exchange.create_market_sell_order(trade["symbol"], qty)
                    else:
                        self.exchange.create_market_buy_order(trade["symbol"], qty)
                except Exception as exc:
                    logger.error(f"close_position market order failed: {exc}")
                    raise

        logger.info(
            f"[{'PAPER' if trade.get('is_paper') else 'LIVE'}] Closed {direction} "
            f"{trade['symbol']} @ {exit_price} PNL={pnl:.4f} ({pnl_pct:.2f}%) reason={reason}"
        )
        return trade

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol (live mode only)."""
        if self.exchange is None:
            return False
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info(f"Leverage set to {leverage}x for {symbol}")
            return True
        except Exception as exc:
            logger.warning(f"set_leverage failed for {symbol}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Account helpers
    # ------------------------------------------------------------------

    def get_balance(self) -> Optional[float]:
        """Return current USDT balance (None on error / paper mode)."""
        if self.exchange is None:
            return None
        try:
            balance = self.exchange.fetch_balance()
            return float(balance["USDT"]["free"])
        except Exception as exc:
            logger.warning(f"get_balance failed: {exc}")
            return None

    def open_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: float,
        leverage: int = 5,
        is_paper: Optional[bool] = None,
    ) -> dict:
        """Unified open trade method called by TradingBot."""
        if is_paper is None:
            is_paper = self.config.get("trading_mode", "paper") == "paper"
        if not is_paper and self.exchange is not None:
            self.set_leverage(symbol, leverage)

        if direction.lower() == "long":
            return self.open_long(symbol, entry_price, stop_loss, take_profit, quantity, is_paper=is_paper)
        else:
            return self.open_short(symbol, entry_price, stop_loss, take_profit, quantity, is_paper=is_paper)

    def close_trade(self, trade, exit_price: float, reason: str = "exit_signal") -> dict:
        """Unified close trade method called by TradingBot."""
        # Convert ORM trade to dict if needed
        if hasattr(trade, "__dict__"):
            t_dict = {
                "id": getattr(trade, "id", None),
                "symbol": getattr(trade, "symbol", ""),
                "direction": getattr(trade, "direction", "long"),
                "entry_price": getattr(trade, "entry_price", exit_price),
                "quantity": getattr(trade, "quantity", 0.0),
                "is_paper": getattr(trade, "is_paper", True),
            }
        else:
            t_dict = dict(trade)
        return self.close_position(t_dict, exit_price, reason)
