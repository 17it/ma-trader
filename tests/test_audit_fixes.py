"""
Comprehensive verification tests covering security hardening, confirmed bug fixes,
live execution safeguards, backtester parity / zero look-ahead, and caching.
Runs entirely against an isolated temporary SQLite database (never touches data/trading.db).
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# Put backend on sys.path and point DB_PATH at a temp directory BEFORE importing backend modules
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(ROOT_DIR))

_TMP_DIR = tempfile.TemporaryDirectory()
_TMP_DB = os.path.join(_TMP_DIR.name, "test_trading.db")

import database as db_mod  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# Rebind database module to isolated temporary DB
db_mod.DB_PATH = _TMP_DB
db_mod.DATABASE_URL = f"sqlite:///{_TMP_DB}"
db_mod.INITIAL_PASSWORD_FILE = os.path.join(_TMP_DIR.name, "admin_initial_password.txt")
db_mod.engine = create_engine(db_mod.DATABASE_URL, connect_args={"check_same_thread": False})
db_mod.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_mod.engine)
db_mod._TABLES_READY = False
os.environ["ADMIN_PASSWORD"] = "InitPass123"
db_mod.create_tables()

from fastapi.testclient import TestClient  # noqa: E402
import auth as auth_mod  # noqa: E402
import main as main_mod  # noqa: E402
from executor import Executor  # noqa: E402
from strategy import MAStrategy  # noqa: E402
from backtester import Backtester  # noqa: E402
from backtest_cache import save_klines_to_cache, get_cached_klines  # noqa: E402
from data_feed import DataFeed  # noqa: E402
from binance_ws import BinanceWSStreamer  # noqa: E402
from notifier import TelegramNotifier, _redact  # noqa: E402
from trader import TradingBot  # noqa: E402


class TestSecurityAndAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_mod.ensure_tables()
        cls.client_cm = TestClient(main_mod.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        db_mod.engine.dispose()
        _TMP_DIR.cleanup()

    def setUp(self):
        auth_mod._login_fails.clear()

    def _login(self, username: str, password: str):
        return self.client.post("/api/auth/login", json={"username": username, "password": password})

    def test_01_forced_password_change_and_token_rules(self):
        # Legacy default password must NOT work
        r_bad = self._login("admin", "123456")
        self.assertEqual(r_bad.status_code, 401)

        # Login with initial env password succeeds but requires password change
        r_ok = self._login("admin", "InitPass123")
        self.assertEqual(r_ok.status_code, 200)
        data = r_ok.json()
        self.assertTrue(data["must_change_password"])
        token = data["token"]

        # Query-string token must be rejected on REST endpoints
        r_qs = self.client.get(f"/api/auth/me?token={token}")
        self.assertEqual(r_qs.status_code, 401)

        # Header token works on /api/auth/me
        headers = {"Authorization": f"Bearer {token}"}
        r_me = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(r_me.status_code, 200)
        self.assertTrue(r_me.json()["must_change_password"])

        # Protected non-auth endpoint blocked with 403 until password is changed
        r_blocked = self.client.get("/api/settings", headers=headers)
        self.assertEqual(r_blocked.status_code, 403)
        self.assertEqual(r_blocked.json()["code"], "PASSWORD_CHANGE_REQUIRED")

        # Weak password rejected
        r_weak = self.client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": "InitPass123", "new_password": "12345678"},
        )
        self.assertEqual(r_weak.status_code, 400)

        # Strong password accepted and unlocks API
        r_chg = self.client.post(
            "/api/auth/change-password",
            headers=headers,
            json={"old_password": "InitPass123", "new_password": "StrongPass99"},
        )
        self.assertEqual(r_chg.status_code, 200)
        new_token = r_chg.json()["token"]
        self.assertFalse(r_chg.json()["must_change_password"])
        self.__class__.auth_headers = {"Authorization": f"Bearer {new_token}"}
        self.__class__.token = new_token

        # Old token was revoked
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
        # New token can now access /api/settings
        self.assertEqual(self.client.get("/api/settings", headers=self.auth_headers).status_code, 200)

    def test_02_login_brute_force_rate_limit(self):
        for _ in range(5):
            r = self._login("admin", "wrong_pass")
            self.assertEqual(r.status_code, 401)
        r_limited = self._login("admin", "wrong_pass")
        self.assertEqual(r_limited.status_code, 429)

    def test_03_settings_secret_masking_and_validation(self):
        # Save real secrets
        r_save = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={
                "binance_key": "REAL_BINANCE_KEY_9876",
                "binance_secret": "REAL_BINANCE_SECRET_4321",
                "telegram_token": "123456:ABCDEF_SECRET_TOKEN",
                "max_daily_loss": "5",
            },
        )
        self.assertEqual(r_save.status_code, 200)
        body = r_save.json()
        # Response must mask all secrets
        self.assertEqual(body["binance_key"], "****9876")
        self.assertEqual(body["binance_secret"], "****4321")
        self.assertTrue(body["telegram_token"].startswith("****"))
        self.assertNotIn("REAL_BINANCE", str(body))

        # Re-submitting masked values must NOT overwrite stored DB secrets
        r_resave = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={
                "binance_key": "****9876",
                "binance_secret": "****4321",
                "telegram_token": body["telegram_token"],
                "max_daily_loss": "7",
            },
        )
        self.assertEqual(r_resave.status_code, 200)
        db = db_mod.SessionLocal()
        try:
            from config import get_setting
            self.assertEqual(get_setting(db, "binance_key"), "REAL_BINANCE_KEY_9876")
            self.assertEqual(get_setting(db, "binance_secret"), "REAL_BINANCE_SECRET_4321")
            self.assertEqual(get_setting(db, "max_daily_loss"), "7")
        finally:
            db.close()

        # Invalid proxy scheme rejected
        r_bad_proxy = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={"http_proxy": "file:///etc/passwd"},
        )
        self.assertEqual(r_bad_proxy.status_code, 400)

    def test_04_symbol_validation(self):
        # Invalid symbol & excessive leverage/risk rejected
        r_bad = self.client.post(
            "/api/symbols",
            headers=self.auth_headers,
            json={"symbol": "BTC;DROP", "leverage": 5, "risk_per_trade": 0.01},
        )
        self.assertEqual(r_bad.status_code, 422)

        r_lev = self.client.post(
            "/api/symbols",
            headers=self.auth_headers,
            json={"symbol": "SOLUSDT", "leverage": 100, "risk_per_trade": 0.01},
        )
        self.assertEqual(r_lev.status_code, 422)

        r_risk = self.client.post(
            "/api/symbols",
            headers=self.auth_headers,
            json={"symbol": "SOLUSDT", "leverage": 5, "risk_per_trade": 0.9},
        )
        self.assertEqual(r_risk.status_code, 422)

    def test_05_websocket_first_frame_auth(self):
        # Valid first-frame auth succeeds and receives bot_status
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": self.token})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "bot_status")

    def test_06_simulate_close_empty_body_stats_and_clear(self):
        # B2: simulate_trade works without NameError
        r_sim = self.client.post(
            "/api/simulate",
            headers=self.auth_headers,
            json={"symbol": "BTCUSDT", "direction": "long", "entry_price": 50000.0},
        )
        self.assertEqual(r_sim.status_code, 200)
        trade = r_sim.json()
        trade_id = trade["id"]

        # Duplicate open paper trade on same symbol blocked
        r_dup = self.client.post(
            "/api/simulate",
            headers=self.auth_headers,
            json={"symbol": "BTCUSDT", "direction": "long", "entry_price": 50100.0},
        )
        self.assertEqual(r_dup.status_code, 409)

        # B1: close trade with EMPTY body (no 422!) and resolves real price instead of entry_price
        with patch("routers.trading._resolve_live_price", return_value=51000.0):
            r_close = self.client.post(f"/api/trades/{trade_id}/close", headers=self.auth_headers)
        self.assertEqual(r_close.status_code, 200)
        closed = r_close.json()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["exit_price"], 51000.0)
        self.assertGreater(closed["pnl"], 0)

        # B6: status daily_pnl and stats total_pnl_pct + best_symbol
        r_status = self.client.get("/api/trading/status", headers=self.auth_headers)
        self.assertEqual(r_status.status_code, 200)
        self.assertGreater(r_status.json()["daily_pnl"], 0)

        r_stats = self.client.get("/api/stats", headers=self.auth_headers)
        self.assertEqual(r_stats.status_code, 200)
        stats = r_stats.json()
        self.assertEqual(stats["best_symbol"], "BTCUSDT")
        self.assertAlmostEqual(stats["total_pnl_pct"], stats["total_pnl"] / 10000.0 * 100.0, places=2)

        # T6: cannot delete or clear when an open LIVE trade exists
        db = db_mod.SessionLocal()
        try:
            live_trade = db_mod.Trade(
                symbol="ETHUSDT", direction="long", entry_price=3000.0,
                stop_loss=2950.0, take_profit=3075.0, quantity=1.0,
                status="open", is_paper=False, open_time=datetime.now(timezone.utc),
                timeframe="1m", confirm_count=2, initial_risk=50.0,
            )
            db.add(live_trade)
            db.commit()
            live_id = live_trade.id
        finally:
            db.close()

        self.assertEqual(self.client.delete(f"/api/trades/{live_id}", headers=self.auth_headers).status_code, 400)
        self.assertEqual(self.client.post("/api/trades/clear", headers=self.auth_headers).status_code, 400)

        # Mark live trade closed, then clear_trades succeeds (B3 fixed)
        db = db_mod.SessionLocal()
        try:
            t = db.query(db_mod.Trade).filter_by(id=live_id).first()
            t.status = "closed"
            db.commit()
        finally:
            db.close()
        r_clear = self.client.post("/api/trades/clear", headers=self.auth_headers)
        self.assertEqual(r_clear.status_code, 200)
        self.assertEqual(r_clear.json()["status"], "cleared")

    def test_07_notifier_redacts_secrets(self):
        raw = "Client error for url 'https://api.telegram.org/bot123456:SECRET_ABC/sendMessage' via socks5://user:pass@1.2.3.4:1080"
        cleaned = _redact(raw)
        self.assertNotIn("123456:SECRET_ABC", cleaned)
        self.assertNotIn("user:pass", cleaned)
        self.assertIn("/bot<REDACTED>", cleaned)
        self.assertIn("://***@1.2.3.4:1080", cleaned)


class TestLiveExecutionSafeguards(unittest.TestCase):
    def _mock_exchange(self):
        ex = MagicMock()
        ex.markets = {"BTCUSDT": {}}
        ex.amount_to_precision.side_effect = lambda sym, qty: f"{qty:.3f}"
        ex.price_to_precision.side_effect = lambda sym, price: f"{price:.2f}"
        return ex

    def test_t1_sl_failure_triggers_emergency_close(self):
        ex = self._mock_exchange()
        ex.create_market_buy_order.return_value = {"id": "ord1", "average": 50000.0, "filled": 0.1}
        ex.create_order.side_effect = RuntimeError("SL rejected")

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        with self.assertRaises(RuntimeError):
            exec_.open_long("BTCUSDT", 50000.0, 49500.0, 50750.0, 0.1, is_paper=False)

        # Must have immediately called emergency reduceOnly market sell
        ex.create_market_sell_order.assert_called_once()
        args, kwargs = ex.create_market_sell_order.call_args
        self.assertEqual(args[0], "BTCUSDT")
        self.assertEqual(args[2], {"reduceOnly": True})

    def test_t2_timeout_reconciles_by_client_order_id(self):
        ex = self._mock_exchange()
        ex.create_market_buy_order.side_effect = TimeoutError("read timeout")
        ex.fetch_order.return_value = {"id": "ord_filled", "average": 50010.0, "filled": 0.123}
        ex.create_order.return_value = {"id": "sl_1"}

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        res = exec_.open_long("BTCUSDT", 50000.0, 49500.0, 50750.0, 0.1234, is_paper=False)
        self.assertEqual(res["exchange_order_id"], "ord_filled")
        self.assertEqual(res["sl_order_id"], "sl_1")
        self.assertEqual(res["quantity"], 0.123)
        self.assertEqual(res["entry_price"], 50010.0)

    def test_t4_update_stop_loss_places_new_before_cancelling_old(self):
        ex = self._mock_exchange()
        call_order = []
        ex.create_order.side_effect = lambda *a, **kw: (call_order.append("create"), {"id": "sl_new"})[1]
        ex.cancel_order.side_effect = lambda *a, **kw: call_order.append("cancel")

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        res = exec_.update_stop_loss("BTCUSDT", "long", 50050.0, 0.1, old_sl_order_id="sl_old")
        self.assertTrue(res["updated"])
        self.assertEqual(res["sl_order_id"], "sl_new")
        self.assertEqual(call_order, ["create", "cancel"])

        # If placing new SL fails, old SL must NOT be cancelled
        ex2 = self._mock_exchange()
        ex2.create_order.side_effect = RuntimeError("reject")
        exec2 = Executor(exchange=ex2, config={}, is_paper=False)
        res2 = exec2.update_stop_loss("BTCUSDT", "long", 50050.0, 0.1, old_sl_order_id="sl_old")
        self.assertFalse(res2["updated"])
        self.assertEqual(res2["sl_order_id"], "sl_old")
        ex2.cancel_order.assert_not_called()

    def test_t5_close_position_handles_already_closed_on_exchange(self):
        ex = self._mock_exchange()
        ex.fetch_positions.return_value = [{"symbol": "BTCUSDT", "info": {"symbol": "BTCUSDT", "positionAmt": "0"}}]
        ex.fetch_order.return_value = {"id": "sl_1", "status": "closed", "average": 49510.0, "fee": {"cost": 2.0, "currency": "USDT"}}

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        res = exec_.close_position(
            {"symbol": "BTCUSDT", "direction": "long", "entry_price": 50000.0, "quantity": 0.1, "is_paper": False, "sl_order_id": "sl_1"},
            exit_price=49400.0,
            reason="stop_loss",
        )
        self.assertTrue(res["already_closed_on_exchange"])
        self.assertEqual(res["exit_price"], 49510.0)
        # Must NOT submit a redundant market sell order
        ex.create_market_sell_order.assert_not_called()

    def test_t7_live_datafeed_never_uses_spot_fallback(self):
        ws = BinanceWSStreamer(use_futures=True)
        ws.latest_prices["BTCUSDT"] = 49999.0
        ws.latest_ts["BTCUSDT"] = time.time()
        ws.latest_is_futures["BTCUSDT"] = False  # Spot tick!

        # Live feed (allow_spot_fallback=False) must reject Spot WS price and Spot URLs
        self.assertIsNone(ws.get_cached_price("BTCUSDT", require_futures=True))
        self.assertEqual(ws.get_cached_price("BTCUSDT", require_futures=False), 49999.0)

        feed = DataFeed(use_futures=True, ws_streamer=ws, allow_spot_fallback=False)
        kline_urls = feed._build_kline_urls("BTCUSDT", "1m", 100)
        self.assertTrue(all("fapi.binance.com" in u for u in kline_urls))
        self.assertFalse(any("api/v3" in u for u in kline_urls))


class TestBacktesterParityAndCache(unittest.TestCase):
    def _build_uptrend_df(self, n_bars: int, start_ms: int, step_ms: int, base: float = 100.0, slope: float = 0.05):
        rows = []
        for i in range(n_bars):
            ts = start_ms + i * step_ms
            c = base + i * slope
            o = c - max(slope * 0.6, 0.12)
            h = c + 0.04
            l = o - 0.04
            rows.append([ts, o, h, l, c, 100.0])
        return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])

    def test_zero_lookahead_htf_and_fees_applied(self):
        # Gentle uptrend (0.015%/min) stays within MA25/MA60 anti-chasing bias guard
        t0 = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        ltf = self._build_uptrend_df(260, t0 - 70 * 60_000, 60_000, base=100.0, slope=0.015)
        # Drop sharply near the end so any open trade exits on SL or MA60
        for i in range(180, 260):
            ltf.loc[i, "open"] = ltf.loc[i - 1, "close"]
            ltf.loc[i, "close"] = ltf.loc[i - 1, "close"] - 0.12
            ltf.loc[i, "high"] = ltf.loc[i, "open"] + 0.01
            ltf.loc[i, "low"] = ltf.loc[i, "close"] - 0.01

        htf = self._build_uptrend_df(100, t0 - 70 * 900_000, 900_000, base=90.0, slope=0.15)

        bt = Backtester()
        res = bt.run(
            symbol="BTCUSDT",
            timeframe="1m",
            htf_timeframe="15m",
            start_date="2025-01-02T00:00:00Z",
            end_date="2025-01-02T03:00:00Z",
            confirm_candles=2,
            rr_ratio=1.5,
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            leverage=5,
            fee_rate=0.0004,
            slippage_rate=0.0001,
            df_ltf_override=ltf,
            df_htf_override=htf,
        )
        self.assertGreaterEqual(res["total_trades"], 1)
        self.assertGreater(res["total_fees"], 0.0)

        # Re-running with zero fees/slippage must yield strictly higher total_pnl
        res_nofee = bt.run(
            symbol="BTCUSDT",
            timeframe="1m",
            htf_timeframe="15m",
            start_date="2025-01-02T00:00:00Z",
            end_date="2025-01-02T03:00:00Z",
            confirm_candles=2,
            rr_ratio=1.5,
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            leverage=5,
            fee_rate=0.0,
            slippage_rate=0.0,
            df_ltf_override=ltf,
            df_htf_override=htf,
        )
        self.assertGreater(res_nofee["total_pnl"], res["total_pnl"])

        # Explicit zero look-ahead check:
        # Make HTF strictly bearish BEFORE t0, and flip bullish ONLY on the HTF bar opening at t0
        # (which doesn't close until t0 + 15m). Any entry MUST NOT happen before t0 + 14m!
        htf_bear_then_bull = self._build_uptrend_df(100, t0 - 70 * 900_000, 900_000, base=200.0, slope=-0.2)
        for idx in range(len(htf_bear_then_bull)):
            if int(htf_bear_then_bull.loc[idx, "ts"]) >= t0:
                htf_bear_then_bull.loc[idx, "close"] = 250.0 + (idx - 70) * 5.0
                htf_bear_then_bull.loc[idx, "high"] = htf_bear_then_bull.loc[idx, "close"] + 1.0
        res_noleak = bt.run(
            symbol="BTCUSDT",
            timeframe="1m",
            htf_timeframe="15m",
            start_date="2025-01-02T00:00:00Z",
            end_date="2025-01-02T03:00:00Z",
            confirm_candles=2,
            rr_ratio=1.5,
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            leverage=5,
            fee_rate=0.0004,
            slippage_rate=0.0001,
            df_ltf_override=ltf,
            df_htf_override=htf_bear_then_bull,
        )
        first_closed_htf_ts = datetime.fromtimestamp((t0 + 14 * 60_000) / 1000, tz=timezone.utc).isoformat()
        for tr in res_noleak["trades"]:
            if tr["direction"] == "long":
                self.assertGreaterEqual(tr["entry_time"], first_closed_htf_ts)

    def test_strategy_live_and_backtest_scalar_parity(self):
        t0 = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        ltf = self._build_uptrend_df(80, t0, 60_000, base=100.0, slope=0.04)
        htf = self._build_uptrend_df(70, t0 - 60 * 900_000, 900_000, base=90.0, slope=0.1)

        s_live = MAStrategy()
        sig_live = s_live.get_signal(ltf, htf, confirm_candles=2)

        df_ma = MAStrategy().calculate_ma(ltf)
        htf_ma = MAStrategy().calculate_ma(htf)
        htf_dir = MAStrategy.check_htf_scalars(
            float(htf_ma.iloc[-1]["close"]),
            float(htf_ma.iloc[-1]["ma7"]),
            float(htf_ma.iloc[-1]["ma25"]),
            float(htf_ma.iloc[-1]["ma60"]),
        )
        s_bt = MAStrategy()
        last = df_ma.iloc[-1]
        sig_bt = s_bt.evaluate_bar(
            float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"]),
            (float(df_ma.iloc[-3]["ma7"]), float(df_ma.iloc[-2]["ma7"]), float(last["ma7"])),
            (float(df_ma.iloc[-3]["ma25"]), float(df_ma.iloc[-2]["ma25"]), float(last["ma25"])),
            float(last["ma60"]), float(last["atr14"]), htf_dir, confirm_candles=2,
        )
        self.assertEqual(sig_live, sig_bt)

    def test_upgraded_strategy_atr_adverse_wick_and_ma25_exit(self):
        # 1. ATR(14) adaptive bias: |close - ma25| <= 2.5 * atr14 passes; > 2.5 * atr14 blocks
        self.assertFalse(MAStrategy.check_overextended_scalars(close=102.0, ma25=100.0, atr14=1.0))
        self.assertTrue(MAStrategy.check_overextended_scalars(close=103.0, ma25=100.0, atr14=1.0))

        # 2. Adverse-wick-only filter: long lower wick on LONG is allowed; long upper wick on LONG is blocked
        self.assertFalse(MAStrategy.check_false_breakout_scalars(o=100.0, h=101.2, l=95.0, c=101.0, direction="long"))
        self.assertTrue(MAStrategy.check_false_breakout_scalars(o=100.0, h=104.0, l=99.8, c=101.0, direction="long"))
        # Symmetric for SHORT: long upper wick allowed; long lower wick blocked
        self.assertFalse(MAStrategy.check_false_breakout_scalars(o=101.0, h=106.0, l=99.8, c=100.0, direction="short"))
        self.assertTrue(MAStrategy.check_false_breakout_scalars(o=101.0, h=101.2, l=97.0, c=100.0, direction="short"))

        # 3. Entry-bar-only wick check does NOT reset confirm_count mid-sequence
        st = MAStrategy()
        ma7_seq = (100.0, 100.5, 101.0)
        ma25_seq = (99.0, 99.3, 99.6)
        # Bar 1: clean long -> 1/3
        s1 = st.evaluate_bar(101.0, 102.2, 100.9, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, "long", confirm_candles=3)
        self.assertEqual(s1["confirm"], 1)
        self.assertTrue(s1.get("pending"))
        # Bar 2: huge upper wick on intermediate bar -> must still advance to 2/3 (NOT reset!)
        s2 = st.evaluate_bar(101.5, 106.0, 101.4, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, "long", confirm_candles=3)
        self.assertEqual(s2["confirm"], 2)
        self.assertTrue(s2.get("pending"))
        # Bar 3: huge upper wick on entry bar -> blocks entry (pending+blocked_by_wick) WITHOUT resetting count
        s3 = st.evaluate_bar(101.5, 106.0, 101.4, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, "long", confirm_candles=3)
        self.assertEqual(s3["confirm"], 3)
        self.assertTrue(s3.get("pending"))
        self.assertTrue(s3.get("blocked_by_wick"))
        # Bar 4: clean bar immediately opens without re-counting from 1
        s4 = st.evaluate_bar(101.0, 102.2, 100.9, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, "long", confirm_candles=3)
        self.assertEqual(s4["confirm"], 3)
        self.assertNotIn("pending", s4)

        # 4. Exit logic: NO MA60 exit when not trailing; exits on MA25 when trailing
        self.assertIsNone(MAStrategy.evaluate_exit_bar("long", close=95.0, ma25=100.0, trailing_active=False))
        self.assertEqual(MAStrategy.evaluate_exit_bar("long", close=99.0, ma25=100.0, trailing_active=True), "ma25_trailing")
        self.assertEqual(MAStrategy.evaluate_exit_bar("short", close=101.0, ma25=100.0, trailing_active=True), "ma25_trailing")
        self.assertIsNone(MAStrategy.evaluate_exit_bar("long", close=101.0, ma25=100.0, trailing_active=True))

        # 5. HTF trend stays 'long' on healthy pullback (close < ma7 while ma7 > ma25 > ma60)
        self.assertEqual(MAStrategy.check_htf_scalars(close=60093.2, ma7=60184.0, ma25=59657.0, ma60=59059.0), "long")
        self.assertEqual(MAStrategy.check_htf_scalars(close=60300.0, ma7=59000.0, ma25=59500.0, ma60=60000.0), "short")

        # 6. Fee- and slippage-aware position sizing keeps total stop-loss + friction <= risk_pct
        cap = 10_000.0
        risk_pct = 0.012
        entry = 60000.0
        sl_dist = 200.0
        qty = MAStrategy.calc_position_size(cap, risk_pct, entry, sl_dist, leverage=8, fee_rate=0.0004, slippage_rate=0.0001)
        worst_loss = qty * (sl_dist + entry * (2 * 0.0004 + 2 * 0.0001))
        self.assertLessEqual(worst_loss, cap * risk_pct + 0.05)
        self.assertLess(qty, round((cap * risk_pct) / sl_dist, 6))

    def test_cache_rejects_unclosed_forming_candle(self):
        now_ms = int(time.time() * 1000)
        closed_ts = ((now_ms // 60_000) - 5) * 60_000
        forming_ts = (now_ms // 60_000) * 60_000  # open_time + 60s > now_ms
        saved = save_klines_to_cache("TESTCACHE", "1m", [
            [closed_ts, 1.0, 2.0, 0.5, 1.5, 10.0],
            [forming_ts, 1.5, 2.5, 1.0, 2.0, 20.0],
        ])
        self.assertEqual(saved, 1)
        rows = get_cached_klines("TESTCACHE", "1m", closed_ts - 60_000, forming_ts + 60_000)
        self.assertEqual([r[0] for r in rows], [closed_ts])


if __name__ == "__main__":
    unittest.main(verbosity=2)
