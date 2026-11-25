"""
MA Trader - Order Execution Layer
Supports both live (ccxt/Binance Futures) and paper trading modes.

Safety guarantees for live mode:
  1. Idempotent entry orders via newClientOrderId + post-timeout reconciliation.
  2. Stop-loss order placed immediately after fill (with retry); if SL placement
     still fails, the position is immediately market-closed to avoid unprotected exposure.
  3. Trailing / tiered SL updates place the NEW stop-loss order BEFORE cancelling
     the old one so there is never an unprotected gap.
  4. Close orders verify live position existence first and fall back to reading the
     SL fill if the exchange already closed the position.
"""
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _clean_symbol(symbol: str) -> str:
    return (symbol or "").replace("/", "").replace(":USDT", "").upper()


def _make_client_order_id(prefix: str, symbol: str) -> str:
    """Build a Binance-compatible clientOrderId (<= 32 chars, alphanumeric + _)."""
    sym = _clean_symbol(symbol)[:10]
    ts = int(time.time() * 1000) % 10_000_000_000
    rand = secrets.token_hex(3)
    return f"ma_{prefix}_{sym}_{ts}_{rand}"[:32]


def _extract_usdt_fee(order: Optional[dict]) -> float:
    """Extract total fee in USDT from a ccxt order response if present."""
    if not isinstance(order, dict):
        return 0.0
    total = 0.0
    fee = order.get("fee")
    if isinstance(fee, dict) and fee.get("cost"):
        if str(fee.get("currency") or "").upper() in {"USDT", "USDC", "BUSD", ""}:
            try:
                total += float(fee["cost"])
            except (TypeError, ValueError):
                pass
    fees = order.get("fees")
    if total == 0.0 and isinstance(fees, list):
        for f in fees:
            if isinstance(f, dict) and f.get("cost"):
                if str(f.get("currency") or "").upper() in {"USDT", "USDC", "BUSD", ""}:
                    try:
                        total += float(f["cost"])
                    except (TypeError, ValueError):
                        pass
    return max(0.0, total)


class Executor:
    """
    Handles order placement, stop-loss synchronization, and position reconciliation
    for both live and paper trading.
    """

    def __init__(self, exchange, config: dict, is_paper: Optional[bool] = None):
        self.exchange = exchange
        self.config = config
        if is_paper is not None:
            self.is_paper = bool(is_paper)
        else:
            self.is_paper = config.get("trading_mode", "paper") == "paper"

    def _ensure_markets(self) -> None:
        """Load exchange markets once so ccxt precision helpers work."""
        if self.exchange is not None and not getattr(self.exchange, "markets", None):
            try:
                self.exchange.load_markets()
            except Exception as exc:
                logger.warning("load_markets failed: %s", exc)

    def _fmt_qty(self, symbol: str, qty: float) -> float:
        """Format order quantity according to exchange stepSize precision."""
        if self.exchange is not None:
            try:
                self._ensure_markets()
                return float(self.exchange.amount_to_precision(symbol, qty))
            except Exception as exc:
                logger.warning("amount_to_precision failed for %s: %s", symbol, exc)
        return round(qty, 4)

    def _fmt_price(self, symbol: str, price: float) -> float:
        """Format order/trigger price according to exchange tickSize precision."""
        if self.exchange is not None:
            try:
                self._ensure_markets()
                return float(self.exchange.price_to_precision(symbol, price))
            except Exception as exc:
                logger.warning("price_to_precision failed for %s: %s", symbol, exc)
        return round(price, 4)

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
        risk_amount = balance * risk_pct
        price_risk = abs(entry - stop_loss)
        if price_risk == 0:
            return 0.0
        qty = risk_amount / price_risk
        return round(qty, 4)

    # ------------------------------------------------------------------
    # Exchange position & order reconciliation helpers
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> Optional[dict]:
        """
        Query the current live Futures position on the exchange for *symbol*.
        Returns:
            {"size": float (abs qty), "side": "long"|"short"|None, "entry_price": float}
            or None if exchange is unavailable / query failed.
        """
        if self.exchange is None or self.is_paper:
            return None
        try:
            self._ensure_markets()
            positions = self.exchange.fetch_positions([symbol])
            target_clean = _clean_symbol(symbol)
            for p in positions or []:
                p_sym = _clean_symbol(p.get("symbol") or p.get("info", {}).get("symbol", ""))
                if p_sym != target_clean:
                    continue
                raw_amt = p.get("info", {}).get("positionAmt")
                if raw_amt is not None:
                    amt = float(raw_amt)
                else:
                    contracts = float(p.get("contracts") or 0.0)
                    side_s = str(p.get("side") or "").lower()
                    amt = -contracts if side_s == "short" else contracts
                if abs(amt) > 0:
                    return {
                        "size": abs(amt),
                        "side": "long" if amt > 0 else "short",
                        "entry_price": float(p.get("entryPrice") or p.get("info", {}).get("entryPrice") or 0.0),
                    }
            return {"size": 0.0, "side": None, "entry_price": 0.0}
        except Exception as exc:
            logger.warning("get_position(%s) failed: %s", symbol, exc)
            return None

    def fetch_order_fill(self, symbol: str, order_id: Optional[str]) -> Optional[dict]:
        """Fetch an order by exchange order_id to inspect fill status & average price."""
        if self.exchange is None or not order_id:
            return None
        try:
            return self.exchange.fetch_order(order_id, symbol)
        except Exception as exc:
            logger.debug("fetch_order(%s, %s) failed: %s", symbol, order_id, exc)
            return None

    def _reconcile_order_by_client_id(self, symbol: str, client_order_id: str) -> Optional[dict]:
        """Check whether a timed-out entry order actually reached & filled on Binance."""
        if self.exchange is None:
            return None
        try:
            order = self.exchange.fetch_order(None, symbol, {"origClientOrderId": client_order_id})
            if isinstance(order, dict) and float(order.get("filled") or 0.0) > 0:
                return order
        except Exception as exc:
            logger.debug("Reconcile by origClientOrderId=%s failed: %s", client_order_id, exc)
        return None

    def _place_sl_order(self, symbol: str, direction: str, qty: float, stop_loss: float) -> Optional[dict]:
        """Place a reduceOnly STOP_MARKET order with one retry."""
        sl_side = "sell" if direction == "long" else "buy"
        sl_price = self._fmt_price(symbol, stop_loss)
        sl_qty = self._fmt_qty(symbol, qty)
        sl_params = {"stopPrice": sl_price, "reduceOnly": True}
        last_exc = None
        for attempt in range(2):
            try:
                return self.exchange.create_order(symbol, "stop_market", sl_side, sl_qty, None, sl_params)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "SL order attempt %d/2 failed for %s (stopPrice=%s): %s",
                    attempt + 1, symbol, sl_price, exc,
                )
                if attempt == 0:
                    time.sleep(0.5)
        if last_exc:
            raise last_exc
        return None

    def _emergency_close(self, symbol: str, direction: str, qty: float) -> None:
        """Immediately market-close a position when protective SL placement fails."""
        close_qty = self._fmt_qty(symbol, qty)
        try:
            if direction == "long":
                self.exchange.create_market_sell_order(symbol, close_qty, {"reduceOnly": True})
            else:
                self.exchange.create_market_buy_order(symbol, close_qty, {"reduceOnly": True})
            logger.warning("[LIVE] Emergency market-closed %s %s qty=%s after SL failure", direction, symbol, close_qty)
        except Exception as exc:
            logger.critical(
                "[LIVE] CRITICAL: Emergency close FAILED for %s %s qty=%s! Manual intervention required! Error: %s",
                direction, symbol, close_qty, exc,
            )

    def update_stop_loss(
        self,
        symbol: str,
        direction: str,
        new_sl: float,
        qty: float,
        old_sl_order_id: Optional[str] = None,
    ) -> dict:
        """
        Safely move the exchange stop-loss order (live mode).
        Places the NEW stop-loss order FIRST, and only cancels the old order after
        the new one is confirmed on the exchange (zero unprotected window).
        """
        if self.is_paper or self.exchange is None:
            return {"updated": True, "sl_order_id": old_sl_order_id, "stop_loss": new_sl}

        formatted_sl = self._fmt_price(symbol, new_sl)
        try:
            new_order = self._place_sl_order(symbol, direction, qty, formatted_sl)
            new_id = new_order.get("id") if isinstance(new_order, dict) else None
        except Exception as exc:
            logger.warning("update_stop_loss: failed to place new SL %s for %s (keeping old SL %s): %s",
                           formatted_sl, symbol, old_sl_order_id, exc)
            return {"updated": False, "sl_order_id": old_sl_order_id, "stop_loss": formatted_sl, "error": str(exc)}

        if old_sl_order_id and old_sl_order_id != new_id:
            try:
                self.exchange.cancel_order(old_sl_order_id, symbol)
            except Exception as exc:
                logger.warning("update_stop_loss: new SL %s placed, failed to cancel old SL %s: %s",
                               new_id, old_sl_order_id, exc)

        logger.info("[LIVE] Updated SL for %s (%s) -> %s (order_id=%s)", symbol, direction, formatted_sl, new_id)
        return {"updated": True, "sl_order_id": new_id, "stop_loss": formatted_sl}

    # ------------------------------------------------------------------
    # Open positions
    # ------------------------------------------------------------------

    def _open_live_position(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
    ) -> dict:
        if self.exchange is None:
            raise RuntimeError("Live exchange instance is not initialized")

        formatted_qty = self._fmt_qty(symbol, qty)
        formatted_sl = self._fmt_price(symbol, stop_loss)
        if formatted_qty <= 0:
            raise ValueError(f"Formatted quantity for {symbol} is <= 0 (raw={qty})")

        client_oid = _make_client_order_id("op", symbol)
        order = None
        try:
            if direction == "long":
                order = self.exchange.create_market_buy_order(
                    symbol, formatted_qty, {"newClientOrderId": client_oid}
                )
            else:
                order = self.exchange.create_market_sell_order(
                    symbol, formatted_qty, {"newClientOrderId": client_oid}
                )
        except Exception as exc:
            logger.warning("Entry order error for %s (cid=%s): %s. Reconciling...", symbol, client_oid, exc)
            order = self._reconcile_order_by_client_id(symbol, client_oid)
            if order is None:
                logger.error("open_%s confirmed not filled for %s: %s", direction, symbol, exc)
                raise

        filled_qty = float(order.get("filled") or formatted_qty)
        if filled_qty <= 0:
            raise RuntimeError(f"Entry order for {symbol} returned filled=0")
        fill_price = float(order.get("average") or order.get("price") or entry)
        entry_fee = _extract_usdt_fee(order)

        # Place protective SL order; if it fails, immediately close the position!
        try:
            sl_order = self._place_sl_order(symbol, direction, filled_qty, formatted_sl)
            sl_order_id = sl_order.get("id") if isinstance(sl_order, dict) else None
        except Exception as sl_exc:
            logger.error(
                "[LIVE] Protective SL placement failed for %s after fill (qty=%s @ %s): %s. "
                "Triggering emergency market close!",
                symbol, filled_qty, fill_price, sl_exc,
            )
            self._emergency_close(symbol, direction, filled_qty)
            raise RuntimeError(f"保护性止损单挂单失败，已自动紧急平仓 {symbol}: {sl_exc}") from sl_exc

        logger.info(
            "[LIVE] Opened %s %s qty=%s @ %s SL=%s (oid=%s sl_oid=%s)",
            direction, symbol, filled_qty, fill_price, formatted_sl, order.get("id"), sl_order_id,
        )
        return {
            "symbol": symbol,
            "direction": direction,
            "entry_price": fill_price,
            "stop_loss": formatted_sl,
            "take_profit": take_profit,
            "quantity": filled_qty,
            "status": "open",
            "is_paper": False,
            "open_time": datetime.now(timezone.utc),
            "exchange_order_id": order.get("id"),
            "sl_order_id": sl_order_id,
            "entry_fee": entry_fee,
        }

    def open_long(
        self,
        symbol: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        is_paper: bool = False,
    ) -> dict:
        if is_paper:
            logger.info("[PAPER] Open long %s qty=%s @ %s SL=%s TP=%s", symbol, qty, entry, stop_loss, take_profit)
            return {
                "symbol": symbol,
                "direction": "long",
                "entry_price": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "quantity": qty,
                "status": "open",
                "is_paper": True,
                "open_time": datetime.now(timezone.utc),
            }
        return self._open_live_position(symbol, "long", entry, stop_loss, take_profit, qty)

    def open_short(
        self,
        symbol: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        is_paper: bool = False,
    ) -> dict:
        if is_paper:
            logger.info("[PAPER] Open short %s qty=%s @ %s SL=%s TP=%s", symbol, qty, entry, stop_loss, take_profit)
            return {
                "symbol": symbol,
                "direction": "short",
                "entry_price": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "quantity": qty,
                "status": "open",
                "is_paper": True,
                "open_time": datetime.now(timezone.utc),
            }
        return self._open_live_position(symbol, "short", entry, stop_loss, take_profit, qty)

    # ------------------------------------------------------------------
    # Close positions
    # ------------------------------------------------------------------

    def close_position(self, trade: dict, exit_price: float, reason: str) -> dict:
        """
        Close an open position and calculate realized P&L.
        In live mode:
          - Checks exchange position first; if already closed (e.g. SL filled on exchange),
            records actual SL fill price instead of looping on reduceOnly rejections.
          - Otherwise submits reduceOnly market close BEFORE cancelling SL (with fallback
            to cancel+close+re-arm SL on failure) and uses actual average fill price.
        """
        entry = float(trade["entry_price"])
        qty = float(trade["quantity"])
        direction = trade["direction"]
        symbol = trade["symbol"]
        actual_exit_price = float(exit_price)
        exit_fee = 0.0
        already_closed_on_exchange = False

        if not trade.get("is_paper") and self.exchange is not None:
            sl_order_id = trade.get("sl_order_id")

            # 1. Check whether exchange already closed the position (e.g. SL triggered)
            pos_info = self.get_position(symbol)
            if pos_info is not None and pos_info["size"] == 0.0:
                already_closed_on_exchange = True
                logger.info("[LIVE] Position %s already closed on exchange; syncing locally.", symbol)
                sl_order = self.fetch_order_fill(symbol, sl_order_id)
                if isinstance(sl_order, dict):
                    avg = float(sl_order.get("average") or sl_order.get("price") or 0.0)
                    if avg > 0:
                        actual_exit_price = avg
                    exit_fee = _extract_usdt_fee(sl_order)
            else:
                close_qty = self._fmt_qty(symbol, pos_info["size"] if pos_info and pos_info["size"] > 0 else qty)
                close_order = None
                try:
                    # Submit reduceOnly market close while SL is still active (no unprotected gap)
                    if direction == "long":
                        close_order = self.exchange.create_market_sell_order(symbol, close_qty, {"reduceOnly": True})
                    else:
                        close_order = self.exchange.create_market_buy_order(symbol, close_qty, {"reduceOnly": True})
                except Exception as first_exc:
                    # Some account modes reject reduceOnly market when a reduceOnly SL occupies full qty.
                    # Cancel SL, retry close, and RE-ARM SL immediately if close still fails!
                    if sl_order_id:
                        logger.warning(
                            "Direct reduceOnly close failed for %s (%s); cancelling SL %s and retrying...",
                            symbol, first_exc, sl_order_id,
                        )
                        try:
                            self.exchange.cancel_order(sl_order_id, symbol)
                        except Exception:
                            pass
                        try:
                            if direction == "long":
                                close_order = self.exchange.create_market_sell_order(
                                    symbol, close_qty, {"reduceOnly": True}
                                )
                            else:
                                close_order = self.exchange.create_market_buy_order(
                                    symbol, close_qty, {"reduceOnly": True}
                                )
                        except Exception as second_exc:
                            # Re-arm stop loss so the position is never left unprotected
                            if trade.get("stop_loss"):
                                try:
                                    self._place_sl_order(symbol, direction, close_qty, float(trade["stop_loss"]))
                                    logger.warning("Re-armed SL for %s after close failure", symbol)
                                except Exception as rearm_exc:
                                    logger.critical("Failed to re-arm SL for %s: %s", symbol, rearm_exc)
                            raise second_exc
                    else:
                        raise first_exc

                if isinstance(close_order, dict):
                    avg = float(close_order.get("average") or close_order.get("price") or 0.0)
                    if avg > 0:
                        actual_exit_price = avg
                    exit_fee = _extract_usdt_fee(close_order)

            # 2. Clean up any resting SL order after position is closed
            if sl_order_id:
                try:
                    self.exchange.cancel_order(sl_order_id, symbol)
                except Exception:
                    pass

        if direction == "long":
            gross_pnl = (actual_exit_price - entry) * qty
        else:
            gross_pnl = (entry - actual_exit_price) * qty

        entry_fee = float(trade.get("entry_fee") or 0.0)
        pnl = gross_pnl - entry_fee - exit_fee
        notional = entry * qty
        pnl_pct = (pnl / notional * 100.0) if notional > 0 else 0.0

        trade["exit_price"] = actual_exit_price
        trade["pnl"] = round(pnl, 4)
        trade["pnl_pct"] = round(pnl_pct, 4)
        trade["fee"] = round(entry_fee + exit_fee, 6)
        trade["close_time"] = datetime.now(timezone.utc)
        trade["close_reason"] = "exchange_closed" if already_closed_on_exchange and reason != "manual" else reason
        trade["status"] = "closed"
        trade["already_closed_on_exchange"] = already_closed_on_exchange

        logger.info(
            "[%s] Closed %s %s @ %s PNL=%.4f (%.2f%%) reason=%s",
            "PAPER" if trade.get("is_paper") else "LIVE",
            direction, symbol, actual_exit_price, pnl, pnl_pct, trade["close_reason"],
        )
        return trade

    # ------------------------------------------------------------------
    # Leverage & Account helpers
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self.exchange is None:
            return False
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info("Leverage set to %dx for %s", leverage, symbol)
            return True
        except Exception as exc:
            logger.warning("set_leverage failed for %s: %s", symbol, exc)
            return False

    def get_balance(self) -> Optional[float]:
        if self.exchange is None:
            return None
        try:
            balance = self.exchange.fetch_balance()
            return float(balance["USDT"]["free"])
        except Exception as exc:
            logger.warning("get_balance failed: %s", exc)
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
        if is_paper is None:
            is_paper = self.is_paper
        if not is_paper and self.exchange is not None:
            self.set_leverage(symbol, leverage)

        if direction.lower() == "long":
            return self.open_long(symbol, entry_price, stop_loss, take_profit, quantity, is_paper=is_paper)
        return self.open_short(symbol, entry_price, stop_loss, take_profit, quantity, is_paper=is_paper)

    def close_trade(self, trade, exit_price: float, reason: str = "exit_signal") -> dict:
        if hasattr(trade, "__dict__") and not isinstance(trade, dict):
            t_dict = {
                "id": getattr(trade, "id", None),
                "symbol": getattr(trade, "symbol", ""),
                "direction": getattr(trade, "direction", "long"),
                "entry_price": getattr(trade, "entry_price", exit_price),
                "stop_loss": getattr(trade, "stop_loss", None),
                "quantity": getattr(trade, "quantity", 0.0),
                "is_paper": getattr(trade, "is_paper", True),
                "sl_order_id": getattr(trade, "sl_order_id", None),
            }
        else:
            t_dict = dict(trade)
        return self.close_position(t_dict, exit_price, reason)
