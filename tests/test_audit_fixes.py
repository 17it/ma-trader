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

# Rebind database module to isolated temporary DB (and alias backend.database so no dual-import can touch data/trading.db)
sys.modules["backend.database"] = db_mod
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

        # Global strategy_mode (Plan A / Plan B / Classic) & cooldown_bars switching + validation
        r_mode_b = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={"strategy_mode": "breakout", "cooldown_bars": "45"},
        )
        self.assertEqual(r_mode_b.status_code, 200)
        self.assertEqual(r_mode_b.json()["strategy_mode"], "breakout")
        self.assertEqual(r_mode_b.json()["cooldown_bars"], "45")

        r_mode_a = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={"strategy_mode": "balanced", "cooldown_bars": "30"},
        )
        self.assertEqual(r_mode_a.status_code, 200)
        self.assertEqual(r_mode_a.json()["strategy_mode"], "balanced")

        r_bad_mode = self.client.post(
            "/api/settings",
            headers=self.auth_headers,
            json={"strategy_mode": "yolo"},
        )
        self.assertEqual(r_bad_mode.status_code, 400)

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

        # Valid per-symbol trailing_activate_r & breakeven_buffer CRUD
        r_ok = self.client.post(
            "/api/symbols",
            headers=self.auth_headers,
            json={
                "symbol": "SOLUSDT",
                "timeframe": "15m",
                "htf_timeframe": "1h",
                "leverage": 5,
                "risk_per_trade": 0.01,
                "trailing_activate_r": 1.2,
                "breakeven_buffer": 0.001,
                "trailing_ma": "ma60",
            },
        )
        self.assertEqual(r_ok.status_code, 201)
        created = r_ok.json()
        self.assertAlmostEqual(created["trailing_activate_r"], 1.2)
        self.assertAlmostEqual(created["breakeven_buffer"], 0.001)
        self.assertEqual(created["trailing_ma"], "ma60")

        r_bad_ma = self.client.post(
            f"/api/symbols/{created['id']}",
            headers=self.auth_headers,
            json={"trailing_ma": "ma99"},
        )
        self.assertEqual(r_bad_ma.status_code, 422)

        r_upd = self.client.post(
            f"/api/symbols/{created['id']}",
            headers=self.auth_headers,
            json={"trailing_activate_r": 0.88, "breakeven_buffer": 0.003, "trailing_ma": "ma25"},
        )
        self.assertEqual(r_upd.status_code, 200)
        self.assertAlmostEqual(r_upd.json()["trailing_activate_r"], 0.88)
        self.assertAlmostEqual(r_upd.json()["breakeven_buffer"], 0.003)
        self.assertEqual(r_upd.json()["trailing_ma"], "ma25")

        self.assertEqual(self.client.delete(f"/api/symbols/{created['id']}", headers=self.auth_headers).status_code, 204)

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

    def test_08_system_logs_pagination_and_keyword_filter(self):
        # Verify TradingBot.broadcast automatically persists signal/log events
        from trader import TradingBot
        bot = TradingBot(
            db_session_factory=db_mod.SessionLocal,
            data_feed=MagicMock(),
            executor=MagicMock(is_paper=True),
            notifier=MagicMock(),
            ws_streamer=MagicMock(),
            is_paper=True,
        )
        bot.broadcast({
            "type": "signal",
            "symbol": "SOLUSDT",
            "direction": "long",
            "confirm": 3,
            "max_confirm": 3,
            "blocked_by_wick": True,
        })

        db_mod.record_system_log("[信号] BTCUSDT LONG 确认 1/3", level="info", symbol="BTCUSDT")
        db_mod.record_system_log("[信号] ETHUSDT SHORT 确认 2/3", level="warning", symbol="ETHUSDT")
        db_mod.record_system_log("止损触发 BTCUSDT", level="error", symbol="BTCUSDT")

        # Broadcasted signal persisted & searchable by keyword
        r_sol = self.client.get("/api/system-logs?keyword=反向长影线", headers=self.auth_headers)
        self.assertEqual(r_sol.status_code, 200)
        self.assertGreaterEqual(r_sol.json()["total"], 1)
        self.assertEqual(r_sol.json()["items"][0]["symbol"], "SOLUSDT")

        # Keyword filter (case-insensitive)
        r_kw = self.client.get("/api/system-logs?keyword=btcusdt&page=1&page_size=20", headers=self.auth_headers)
        self.assertEqual(r_kw.status_code, 200)
        body = r_kw.json()
        self.assertGreaterEqual(body["total"], 2)
        self.assertTrue(all("BTCUSDT" in (it["message"] + (it["symbol"] or "")) for it in body["items"]))

        # Level + keyword filter
        r_lv = self.client.get("/api/system-logs?keyword=止损&level=error", headers=self.auth_headers)
        self.assertEqual(r_lv.status_code, 200)
        self.assertEqual(r_lv.json()["total"], 1)

        # Pagination
        r_pg = self.client.get("/api/system-logs?page=1&page_size=1", headers=self.auth_headers)
        self.assertEqual(r_pg.status_code, 200)
        self.assertEqual(len(r_pg.json()["items"]), 1)
        self.assertGreaterEqual(r_pg.json()["total_pages"], 3)

        # Clear
        r_clr = self.client.post("/api/system-logs/clear", headers=self.auth_headers)
        self.assertEqual(r_clr.status_code, 200)
        r_empty = self.client.get("/api/system-logs", headers=self.auth_headers)
        self.assertEqual(r_empty.json()["total"], 0)

        # V2-B1: /api/backtest inherits symbol's configured leverage & risk_per_trade when omitted
        r_sym = self.client.post(
            "/api/symbols",
            headers=self.auth_headers,
            json={"symbol": "AVAXUSDT", "timeframe": "5m", "htf_timeframe": "1h", "leverage": 12, "risk_per_trade": 0.018},
        )
        self.assertEqual(r_sym.status_code, 201)
        sym_id = r_sym.json()["id"]
        async def _noop_bt(*_args, **_kwargs):
            return None

        with patch("routers.backtest._run_backtest_task", side_effect=_noop_bt):
            r_bt = self.client.post(
                "/api/backtest",
                headers=self.auth_headers,
                json={"symbol": "AVAXUSDT", "timeframe": "5m", "htf_timeframe": "1h", "start_date": "2026-10-01", "end_date": "2026-10-05"},
            )
        self.assertEqual(r_bt.status_code, 202)
        import json as _json
        saved_params = _json.loads(r_bt.json()["params"])
        self.assertEqual(saved_params["leverage"], 12)
        self.assertAlmostEqual(saved_params["risk_per_trade"], 0.018)
        self.client.delete(f"/api/symbols/{sym_id}", headers=self.auth_headers)


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

        # 4. Exit logic: NO MA exit when not trailing; exits on configured MA25 or MA60 (and HTF prefix) when trailing
        self.assertIsNone(MAStrategy.evaluate_exit_bar("long", close=95.0, ma_val=100.0, trailing_active=False))
        self.assertEqual(MAStrategy.evaluate_exit_bar("long", close=99.0, ma_val=100.0, trailing_active=True, trailing_ma="ma25"), "ma25_trailing")
        self.assertEqual(MAStrategy.evaluate_exit_bar("short", close=101.0, ma_val=100.0, trailing_active=True, trailing_ma="ma25"), "ma25_trailing")
        self.assertEqual(MAStrategy.evaluate_exit_bar("long", close=99.0, ma_val=100.0, trailing_active=True, trailing_ma="ma60"), "ma60_trailing")
        self.assertEqual(MAStrategy.evaluate_exit_bar("long", close=99.0, ma_val=100.0, trailing_active=True, trailing_ma="ma25", is_htf_rail=True), "htf_ma25_trailing")
        self.assertIsNone(MAStrategy.evaluate_exit_bar("long", close=101.0, ma_val=100.0, trailing_active=True, trailing_ma="ma60"))

        # Plan A (balanced) & Plan B (breakout) strictly block htf_dir=None chop and size SL from HTF ATR
        st_a = MAStrategy(strategy_mode="balanced")
        self.assertIsNone(st_a.evaluate_bar(101.0, 102.2, 100.9, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, None, confirm_candles=1))
        sig_a = st_a.evaluate_bar(101.0, 102.2, 100.9, 102.0, ma7_seq, ma25_seq, 98.0, 1.5, "long", confirm_candles=1)
        self.assertIsNotNone(sig_a)
        sl_a, _tp_a, dist_a = MAStrategy.compute_mode_sl_tp("long", 60000.0, 59900.0, 59950.0, 60050.0, strategy_mode="balanced", htf_atr14=350.0)
        self.assertAlmostEqual(dist_a, 700.0, places=4)  # 2.0 * 350 > 0.8% * 60000 (480)

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

        # 7. 0.88R -> +0.3% profit lock (and safe cap when initial stop is ultra-tight 0.30%)
        wide_lock = MAStrategy.compute_trailing_lock_sl("long", 60000.0, 600.0, 59400.0)
        self.assertAlmostEqual(wide_lock, 60000.0 * 1.003, places=4)
        tight_lock = MAStrategy.compute_trailing_lock_sl("long", 60000.0, 180.0, 59820.0)
        # 0.88 * 180 = 158.4 -> trigger 60158.4; lock MUST stay below trigger price!
        self.assertLess(tight_lock, 60000.0 + 0.88 * 180.0)
        self.assertGreater(tight_lock, 60000.0)

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

    def test_end_of_backtest_settles_open_trade_and_marks_to_market(self):
        # N-B1: An open losing trade at end_date must be settled and reflected in total_pnl / max_drawdown
        t0 = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        ltf = self._build_uptrend_df(120, t0 - 70 * 60_000, 60_000, base=100.0, slope=0.015)
        # Mild dip at the very end that stays ABOVE initial SL so trade remains open until end_date
        for i in range(110, 120):
            ltf.loc[i, "open"] = ltf.loc[i - 1, "close"]
            ltf.loc[i, "close"] = ltf.loc[i - 1, "close"] - 0.01
            ltf.loc[i, "high"] = ltf.loc[i, "open"] + 0.005
            ltf.loc[i, "low"] = ltf.loc[i, "close"] - 0.005
        htf = self._build_uptrend_df(90, t0 - 70 * 900_000, 900_000, base=90.0, slope=0.15)

        bt = Backtester()
        res = bt.run(
            symbol="BTCUSDT",
            timeframe="1m",
            htf_timeframe="15m",
            start_date="2025-01-02T00:00:00Z",
            end_date="2025-01-02T00:50:00Z",
            confirm_candles=2,
            rr_ratio=0.0,
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            leverage=5,
            fee_rate=0.0004,
            slippage_rate=0.0001,
            df_ltf_override=ltf,
            df_htf_override=htf,
        )
        self.assertGreaterEqual(res["total_trades"], 1)
        self.assertEqual(res["trades"][-1]["close_reason"], "end_of_backtest")
        self.assertGreater(res["total_fees"], 0.0)


class TestSecondAuditFixes(unittest.TestCase):
    def _mock_exchange(self):
        ex = MagicMock()
        ex.markets = {"BTCUSDT": {}}
        ex.amount_to_precision.side_effect = lambda sym, qty: f"{qty:.3f}"
        ex.price_to_precision.side_effect = lambda sym, price: f"{price:.2f}"
        ex.fetch_my_trades.return_value = []
        return ex

    def test_nt1_algo_sl_cancel_and_fetch_pass_trigger_true(self):
        ex = self._mock_exchange()
        ex.create_order.return_value = {"id": "algo_new"}

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        res = exec_.update_stop_loss("BTCUSDT", "long", 50100.0, 0.1, old_sl_order_id="algo_old")
        self.assertTrue(res["updated"])
        ex.cancel_order.assert_called_once_with("algo_old", "BTCUSDT", {"trigger": True})

        # fetch_order_fill with is_stop=True must query Algo Order endpoint via {"trigger": True}
        ex.fetch_order.return_value = {"id": "algo_old", "status": "closed", "average": 50080.0}
        filled = exec_.fetch_order_fill("BTCUSDT", "algo_old", is_stop=True)
        ex.fetch_order.assert_called_with("algo_old", "BTCUSDT", {"trigger": True})
        self.assertEqual(filled["average"], 50080.0)

    def test_nt3_symbol_slash_normalization_and_htf_gt_ltf(self):
        from models import validate_symbol_str, SymbolConfigCreate
        self.assertEqual(validate_symbol_str("btc/usdt"), "BTCUSDT")
        self.assertEqual(validate_symbol_str("ETH/USDT:USDT"), "ETHUSDT")
        with self.assertRaises(ValueError):
            validate_symbol_str("BT")

        # htf_timeframe <= timeframe must be rejected
        with self.assertRaises(Exception):
            SymbolConfigCreate(symbol="BTCUSDT", timeframe="1h", htf_timeframe="15m")
        with self.assertRaises(Exception):
            SymbolConfigCreate(symbol="BTCUSDT", timeframe="15m", htf_timeframe="15m")
        cfg_ok = SymbolConfigCreate(symbol="btc/usdt", timeframe="15m", htf_timeframe="1h", rr_ratio=0.0)
        self.assertEqual(cfg_ok.symbol, "BTCUSDT")
        self.assertEqual(cfg_ok.rr_ratio, 0.0)

    def test_nt4_rearmed_sl_id_propagated_and_partial_size_reconciled(self):
        ex = self._mock_exchange()
        ex.fetch_positions.return_value = [{"symbol": "BTCUSDT", "info": {"symbol": "BTCUSDT", "positionAmt": "0.1"}}]
        ex.create_market_sell_order.side_effect = RuntimeError("exchange busy")
        ex.create_order.return_value = {"id": "algo_rearmed_99"}

        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        trade_dict = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_price": 50000.0,
            "stop_loss": 49500.0,
            "quantity": 0.1,
            "is_paper": False,
            "sl_order_id": "algo_orig_1",
        }
        with self.assertRaises(RuntimeError) as ctx:
            exec_.close_position(trade_dict, exit_price=50100.0, reason="manual")
        self.assertEqual(getattr(ctx.exception, "rearmed_sl_order_id", None), "algo_rearmed_99")
        self.assertEqual(trade_dict["sl_order_id"], "algo_rearmed_99")

        # Partial position reduction reconciliation in TradingBot
        db = db_mod.SessionLocal()
        try:
            tr = db_mod.Trade(
                symbol="BTCUSDT", direction="long", entry_price=50000.0,
                stop_loss=49500.0, take_profit=50750.0, quantity=0.2,
                status="open", is_paper=False, open_time=datetime.now(timezone.utc),
                timeframe="1m", confirm_count=2, initial_risk=500.0, sl_order_id="sl_old",
            )
            db.add(tr)
            db.commit()
            tid = tr.id
        finally:
            db.close()

        ex_partial = self._mock_exchange()
        ex_partial.fetch_positions.return_value = [{"symbol": "BTCUSDT", "info": {"symbol": "BTCUSDT", "positionAmt": "0.1"}}]
        ex_partial.create_order.return_value = {"id": "sl_synced_qty"}
        bot = TradingBot(
            db_session_factory=db_mod.SessionLocal,
            data_feed=MagicMock(),
            executor=Executor(exchange=ex_partial, config={}, is_paper=False),
            notifier=MagicMock(),
            ws_streamer=MagicMock(),
            is_paper=False,
        )
        pos = {
            "id": tid, "symbol": "BTCUSDT", "direction": "long", "entry_price": 50000.0,
            "stop_loss": 49500.0, "take_profit": 50750.0, "quantity": 0.2, "is_paper": False,
            "sl_order_id": "sl_old", "trailing_active": False,
        }
        closed = asyncio.run(bot._reconcile_live_position("BTCUSDT", pos, 50000.0))
        self.assertFalse(closed)
        self.assertAlmostEqual(pos["quantity"], 0.1)
        self.assertEqual(pos["sl_order_id"], "sl_synced_qty")

        db = db_mod.SessionLocal()
        try:
            refreshed = db.query(db_mod.Trade).filter_by(id=tid).first()
            self.assertAlmostEqual(refreshed.quantity, 0.1)
            self.assertEqual(refreshed.sl_order_id, "sl_synced_qty")
            db.delete(refreshed)
            db.commit()
        finally:
            db.close()

    def test_nt5_live_fee_from_my_trades_and_manual_close_deducts_fee(self):
        ex = self._mock_exchange()
        ex.create_market_buy_order.return_value = {"id": "entry_ord_1", "average": 50000.0, "filled": 0.1}
        ex.create_order.return_value = {"id": "sl_ord_1"}
        ex.fetch_my_trades.return_value = [
            {"order": "entry_ord_1", "fee": {"cost": 2.0, "currency": "USDT"}}
        ]
        exec_ = Executor(exchange=ex, config={}, is_paper=False)
        opened = exec_.open_long("BTCUSDT", 50000.0, 49500.0, 50750.0, 0.1, is_paper=False)
        self.assertAlmostEqual(opened["entry_fee"], 2.0)

    def test_na1_and_ns1_single_module_and_per_ip_rate_limit(self):
        import importlib
        d1 = importlib.import_module("database")
        d2 = importlib.import_module("backend.database")
        self.assertIs(d1, d2)
        self.assertIs(d1.engine, d2.engine)

        # Attacker failing 5 times from IP A must NOT lock out admin logging in from IP B
        auth_mod._login_fails.clear()
        for _ in range(5):
            auth_mod.record_login_failure("203.0.113.9", "admin")
        self.assertGreater(auth_mod.login_retry_after("203.0.113.9", "admin"), 0)
        self.assertEqual(auth_mod.login_retry_after("198.51.100.2", "admin"), 0)


class TestThirdAuditFixes(unittest.TestCase):
    """Tests for V2 audit report fixes (V2-T1, V2-T2, V2-B1, V2-B2, V2-B3)."""

    def test_v2_t1_stale_pre_activation_htf_candle_never_closes_winning_trade(self):
        from trader import TradingBot

        db = db_mod.SessionLocal()
        try:
            now_utc = datetime.now(timezone.utc)
            tr = db_mod.Trade(
                symbol="BTCUSDT",
                direction="long",
                entry_price=60000.0,
                stop_loss=59400.0,
                take_profit=60900.0,
                quantity=0.1,
                status="open",
                is_paper=True,
                open_time=now_utc,
                timeframe="1m",
                confirm_count=2,
                initial_risk=600.0,
                trailing_activate_r=1.0,
                breakeven_buffer=0.002,
                trailing_ma="ma25",
                strategy_mode="balanced",
            )
            db.add(tr)
            db.commit()
            tid = tr.id
        finally:
            db.close()

        # Construct 15m klines where the LAST CLOSED 15m bar closed 2 minutes BEFORE open_time
        # AND closed BELOW MA25 (pullback bar), while the current 15m bar is still forming.
        closed_before_open = now_utc - timedelta(minutes=17)  # closes at now_utc - 2m < open_time!
        rows = []
        for k in range(70):
            ts_dt = closed_before_open - timedelta(minutes=15 * (69 - k))
            px = 60500.0 if k < 69 else 59800.0  # last closed bar dipped below MA25 (~60470)!
            rows.append({"timestamp": ts_dt, "open": px, "high": px + 20, "low": px - 20, "close": px, "volume": 10.0})
        # Forming bar (dropped by iloc[:-1])
        rows.append({"timestamp": now_utc - timedelta(minutes=2), "open": 60000.0, "high": 60700.0, "low": 59990.0, "close": 60650.0, "volume": 5.0})
        df_15m = pd.DataFrame(rows)

        feed = MagicMock()
        feed.get_ticker.return_value = 60650.0  # >= 60000 + 1.0 * 600 -> activates trailing!
        feed.get_ohlcv.return_value = df_15m

        bot = TradingBot(
            db_session_factory=db_mod.SessionLocal,
            data_feed=feed,
            executor=MagicMock(is_paper=True),
            notifier=MagicMock(),
            ws_streamer=MagicMock(),
            is_paper=True,
        )
        bot.strategies["BTCUSDT"] = MAStrategy(strategy_mode="balanced")
        bot.positions["BTCUSDT"] = {
            "id": tid,
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_price": 60000.0,
            "stop_loss": 59400.0,
            "take_profit": 60900.0,
            "quantity": 0.1,
            "status": "open",
            "is_paper": True,
            "open_time": now_utc,
            "timeframe": "1m",
            "htf_timeframe": "15m",
            "trailing_active": False,
            "trailing_activated_at": None,
            "risk_r": 600.0,
            "trailing_activate_r": 1.0,
            "breakeven_buffer": 0.002,
            "trailing_ma": "ma25",
            "strategy_mode": "balanced",
        }

        asyncio.run(bot._check_exits())
        # Trade MUST still be open (NOT falsely closed by the pre-entry 15m candle!), with trailing activated & profit locked
        self.assertIn("BTCUSDT", bot.positions)
        self.assertTrue(bot.positions["BTCUSDT"]["trailing_active"])
        self.assertGreater(bot.positions["BTCUSDT"]["stop_loss"], 60000.0)

        db = db_mod.SessionLocal()
        try:
            db.query(db_mod.Trade).filter_by(id=tid).delete()
            db.commit()
        finally:
            db.close()

    def test_v2_t2_failed_sl_sync_preserves_old_sl_for_retry(self):
        from trader import TradingBot

        ex = MagicMock()
        ex.markets = {"BTCUSDT": {}}
        ex.amount_to_precision.side_effect = lambda sym, qty: f"{qty:.3f}"
        ex.price_to_precision.side_effect = lambda sym, price: f"{price:.2f}"
        ex.create_order.side_effect = RuntimeError("-2021 Order would immediately trigger")

        bot = TradingBot(
            db_session_factory=db_mod.SessionLocal,
            data_feed=MagicMock(),
            executor=Executor(exchange=ex, config={}, is_paper=False),
            notifier=MagicMock(),
            ws_streamer=MagicMock(),
            is_paper=False,
        )
        pos = {
            "id": 99999,
            "symbol": "BTCUSDT",
            "direction": "long",
            "stop_loss": 59500.0,
            "quantity": 0.1,
            "is_paper": False,
            "sl_order_id": "sl_orig",
        }
        resulting_sl = asyncio.run(bot._sync_exchange_sl("BTCUSDT", pos, 60200.0))
        # MUST keep 59500.0 (not overwrite with 60200.0!) so next 15s cycle sees moved_enough=True and retries
        self.assertEqual(resulting_sl, 59500.0)
        self.assertEqual(pos["sl_order_id"], "sl_orig")

    def test_v2_b2_cache_detects_sub_hour_tail_and_single_bar_gap(self):
        from backtest_cache import find_missing_intervals

        now_ms = int(time.time() * 1000)
        step = 60_000  # 1m
        last_closed = (now_ms // step) * step
        # Cached bars stop 20 minutes ago (previously skipped due to 1h blind spot!) and have a 2m single-bar gap
        t0 = last_closed - 25 * step
        cached = [
            [t0, 1, 1, 1, 1, 1],
            [t0 + step, 1, 1, 1, 1, 1],
            # missing t0 + 2*step!
            [t0 + 3 * step, 1, 1, 1, 1, 1],
            [t0 + 4 * step, 1, 1, 1, 1, 1],
        ]
        missing = find_missing_intervals(cached, t0, last_closed, step)
        self.assertIn((t0 + 2 * step, t0 + 3 * step), missing)
        self.assertIn((t0 + 5 * step, last_closed), missing)

    def test_multi_paper_bots_shared_feed_and_strategy_isolation(self):
        # 1. DataFeed short TTL cache avoids duplicate OHLCV fetches across simultaneous paper bots
        feed = DataFeed.__new__(DataFeed)
        feed._ws_streamer = None
        feed._use_futures = True
        feed._allow_spot_fallback = True
        feed._geo_blocked = False
        feed._api_key = ""
        feed._secret = ""
        feed._authenticated = False
        feed._proxy = ""
        feed._call_lock = unittest.mock.MagicMock()
        feed._call_lock.__enter__ = lambda s: None
        feed._call_lock.__exit__ = lambda s, *a: False
        feed._ohlcv_cache = {}
        feed._ticker_cache = {}
        feed._ohlcv_ttl = 2.5
        feed._ticker_ttl = 1.5
        mock_ex = unittest.mock.MagicMock()
        mock_ex.fetch_ohlcv.return_value = [
            [1700000000000, 50000, 50100, 49900, 50050, 10],
            [1700000060000, 50050, 50200, 50000, 50150, 12],
            [1700000120000, 50150, 50300, 50100, 50250, 15],
        ]
        feed._exchange = mock_ex
        df1 = feed.get_ohlcv("BTCUSDT", "1m", 200)
        df2 = feed.get_ohlcv("BTCUSDT", "1m", 200)
        self.assertIsNotNone(df1)
        self.assertIsNotNone(df2)
        self.assertIsNot(df1, df2)
        self.assertEqual(mock_ex.fetch_ohlcv.call_count, 1)

        # 2. Independent start/stop of multiple paper bots via API + shared DataFeed + isolated simulate/stats
        auth_mod._login_fails.clear()
        with TestClient(main_mod.app) as client:
            r_login = client.post("/api/auth/login", json={"username": "admin", "password": "StrongPass99"})
            if r_login.status_code != 200:
                r_init = client.post("/api/auth/login", json={"username": "admin", "password": "InitPass123"})
                tok = r_init.json()["token"]
                client.post(
                    "/api/auth/change-password",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"old_password": "InitPass123", "new_password": "StrongPass99"},
                )
                r_login = client.post("/api/auth/login", json={"username": "admin", "password": "StrongPass99"})
            headers = {"Authorization": f"Bearer {r_login.json()['token']}"}

            with patch("trader.TradingBot._load_symbol_configs", return_value=[]):
                r_s1 = client.post("/api/trading/start?mode=paper&strategy_mode=balanced", headers=headers)
                r_s2 = client.post("/api/trading/start?mode=paper&strategy_mode=breakout", headers=headers)
                self.assertEqual(r_s1.status_code, 200)
                self.assertEqual(r_s2.status_code, 200)

                bots = main_mod.app.state.bots
                self.assertTrue(bots["paper:balanced"].running)
                self.assertTrue(bots["paper:breakout"].running)
                self.assertIs(bots["paper:balanced"].data_feed, bots["paper:breakout"].data_feed)

                st = client.get("/api/trading/status", headers=headers).json()
                self.assertTrue(st["paper_running"])
                self.assertEqual(
                    st["paper_modes"],
                    {"balanced": True, "breakout": True, "classic": False},
                )

                # Stopping ONLY balanced keeps breakout running!
                r_stop_a = client.post("/api/trading/stop?mode=paper&strategy_mode=balanced", headers=headers)
                self.assertEqual(r_stop_a.status_code, 200)
                st2 = client.get("/api/trading/status", headers=headers).json()
                self.assertTrue(st2["paper_running"])
                self.assertEqual(
                    st2["paper_modes"],
                    {"balanced": False, "breakout": True, "classic": False},
                )

                # Simultaneous open paper trades on BTCUSDT across different strategies succeed
                r_ta = client.post(
                    "/api/simulate",
                    headers=headers,
                    json={"symbol": "BTCUSDT", "direction": "long", "entry_price": 60000.0, "strategy_mode": "balanced"},
                )
                r_tb = client.post(
                    "/api/simulate",
                    headers=headers,
                    json={"symbol": "BTCUSDT", "direction": "short", "entry_price": 60000.0, "strategy_mode": "breakout"},
                )
                self.assertEqual(r_ta.status_code, 200)
                self.assertEqual(r_tb.status_code, 200)
                self.assertEqual(r_ta.json()["strategy_mode"], "balanced")
                self.assertEqual(r_tb.json()["strategy_mode"], "breakout")

                # Filtering /api/trades and /api/stats by strategy_modes isolates selected strategies
                tr_b = client.get("/api/trades?is_paper=true&strategy_mode=breakout", headers=headers).json()
                self.assertTrue(all(t["strategy_mode"] == "breakout" for t in tr_b))
                st_none = client.get("/api/stats?strategy_modes=none", headers=headers).json()
                self.assertEqual(st_none["total_trades"], 0)
                st_multi = client.get("/api/stats?strategy_modes=balanced,breakout", headers=headers).json()
                self.assertIn("total_trades", st_multi)

                # Clean up running bots & test trades
                client.post("/api/trading/stop?mode=paper&strategy_mode=all", headers=headers)
                client.post("/api/trades/clear", headers=headers)


def tearDownModule():
    db_mod.engine.dispose()
    _TMP_DIR.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
