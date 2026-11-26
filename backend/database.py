"""
SQLAlchemy models and database initialization for MA Auto Trader.
Database: SQLite at data/trading.db
"""

import os
import sys
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Index, UniqueConstraint, text,
    create_engine, event
)
from sqlalchemy.orm import declarative_base, sessionmaker

# Prevent dual-module loading ("database" vs "backend.database") from creating two Engines / ORM Bases
sys.modules.setdefault("database", sys.modules[__name__])
sys.modules.setdefault("backend.database", sys.modules[__name__])

# ---------------------------------------------------------------------------
# Engine setup
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "trading.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,  # required for SQLite + FastAPI
        "timeout": 60.0,              # wait up to 60s for lock release
    },
    echo=False,
)

# Enable WAL mode for high concurrency between web server and background tasks
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class SettingModel(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String)


class UserModel(Base):
    """Web admin user credentials."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    salt = Column(String(64), nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SymbolConfig(Base):
    __tablename__ = "symbol_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, unique=True, nullable=False)       # e.g. "BTCUSDT"
    timeframe = Column(String, default="1m")                   # "1m","5m","15m"
    htf_timeframe = Column(String, default="15m")              # 高时间框架
    confirm_candles = Column(Integer, default=2)               # 连续确认K线数
    leverage = Column(Integer, default=5)
    risk_per_trade = Column(Float, default=0.01)               # 1% 账户风险
    rr_ratio = Column(Float, default=1.5)                      # 盈亏比
    trailing_activate_r = Column(Float, default=0.88)          # 浮盈达多少 R 开启保本与均线追踪
    breakeven_buffer = Column(Float, default=0.003)            # 保本缓冲比例（0.003 = 0.3%）
    trailing_ma = Column(String(10), default="ma25")           # 追踪均线轨道（"ma25" | "ma60"）
    enabled = Column(Boolean, default=True)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False)
    direction = Column(String, nullable=False)                 # "long" or "short"
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    status = Column(String, default="open")                    # "open","closed","stopped"
    close_reason = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    is_paper = Column(Boolean, default=False)
    open_time = Column(DateTime, nullable=False)
    close_time = Column(DateTime, nullable=True)
    timeframe = Column(String, nullable=False)
    confirm_count = Column(Integer, default=0)
    ma7_at_entry = Column(Float, nullable=True)
    ma25_at_entry = Column(Float, nullable=True)
    ma60_at_entry = Column(Float, nullable=True)
    exchange_order_id = Column(String, nullable=True)
    sl_order_id = Column(String, nullable=True)
    trailing_active = Column(Boolean, default=False)
    trailing_activated_at = Column(DateTime, nullable=True)    # 激活移动止盈的时刻，防止读旧 K 线误平仓
    initial_risk = Column(Float, nullable=True)                # 开仓时 1R 距离（|entry - 初始止损|），重启后用于分档止盈
    trailing_activate_r = Column(Float, nullable=True)         # 开仓时该交易对的追踪触发 R
    breakeven_buffer = Column(Float, nullable=True)            # 开仓时该交易对的保本缓冲比例
    trailing_ma = Column(String(10), nullable=True)            # 开仓时该交易对的追踪均线（"ma25" | "ma60"）
    strategy_mode = Column(String(20), nullable=True)          # 开仓时策略模式（"balanced" | "breakout" | "classic"）
    fee = Column(Float, default=0.0)                           # 累计手续费（USDT）


class SessionTokenModel(Base):
    """Persistent login session tokens (survives service restarts)."""
    __tablename__ = "session_tokens"

    token = Column(String(128), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    username = Column(String(50), nullable=False)
    expires_at = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OperationLog(Base):
    """Persistent operation logs (open, close, system start, system stop)."""
    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_type = Column(String(32), nullable=False, index=True)   # "open" | "close" | "start" | "stop"
    level = Column(String(16), default="info", nullable=False)   # "info" | "success" | "warning" | "error"
    symbol = Column(String(32), nullable=True, index=True)
    mode = Column(String(16), nullable=True)                     # "paper" | "live" | None
    title = Column(String(160), nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


def record_operation_log(
    log_type: str,
    title: str,
    message: str,
    *,
    level: str = "info",
    symbol: str | None = None,
    mode: str | None = None,
    db=None,
) -> None:
    """Persist an OperationLog entry safely without disrupting trading execution."""
    own_session = db is None
    session = SessionLocal() if own_session else db
    try:
        entry = OperationLog(
            log_type=log_type,
            level=level,
            symbol=symbol,
            mode=mode,
            title=title,
            message=message,
            created_at=datetime.now(timezone.utc),
        )
        session.add(entry)
        session.commit()
        if entry.id and entry.id % 200 == 0:
            session.execute(
                text("DELETE FROM operation_logs WHERE id <= :cutoff"),
                {"cutoff": entry.id - 20_000},
            )
            session.commit()
    except Exception:
        if own_session:
            session.rollback()
    finally:
        if own_session:
            session.close()


class SystemLog(Base):
    """Persistent system / signal / monitor logs (mirrors homepage logBox with full history)."""
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), default="info", nullable=False, index=True)  # "info" | "success" | "warning" | "error"
    symbol = Column(String(32), nullable=True, index=True)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


def record_system_log(
    message: str,
    *,
    level: str = "info",
    symbol: str | None = None,
    db=None,
) -> None:
    """Persist a SystemLog entry safely without disrupting trading execution."""
    if not message:
        return
    norm_level = (level or "info").lower()
    if norm_level == "danger":
        norm_level = "error"
    if norm_level not in {"info", "success", "warning", "error"}:
        norm_level = "info"

    own_session = db is None
    session = SessionLocal() if own_session else db
    try:
        entry = SystemLog(
            level=norm_level,
            symbol=symbol.upper() if symbol else None,
            message=str(message),
            created_at=datetime.now(timezone.utc),
        )
        session.add(entry)
        session.commit()
        if entry.id and entry.id % 200 == 0:
            session.execute(
                text("DELETE FROM system_logs WHERE id <= :cutoff"),
                {"cutoff": entry.id - 20_000},
            )
            session.commit()
    except Exception:
        if own_session:
            session.rollback()
    finally:
        if own_session:
            session.close()


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=True)                      # 用户自定义回测标题/备注
    symbol = Column(String)
    timeframe = Column(String)
    start_date = Column(String)
    end_date = Column(String)
    params = Column(String)                                    # JSON string
    results = Column(String, nullable=True)                    # JSON string
    status = Column(String, default="pending")                 # "pending","running","done","failed"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    error_msg = Column(String, nullable=True)


class CachedKline(Base):
    """Local SQLite persistent storage for historical OHLCV bars (incremental cache)."""
    __tablename__ = "cached_klines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False)
    timeframe = Column(String(10), nullable=False)
    open_time = Column(Integer, nullable=False)  # timestamp in milliseconds
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

    # The unique constraint already creates an index on (symbol, timeframe, open_time);
    # a second identical index only wastes disk space and slows down inserts.
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uix_kline_sym_tf_ts"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "telegram_token": "",
    "telegram_chat_id": "",
    "binance_key": "",
    "binance_secret": "",
    "http_proxy": "",
    "trading_mode": "paper",
    "max_daily_loss": "5",
    "strategy_mode": "balanced",
    "cooldown_bars": "30",
}


def _get_env_fallback(key: str) -> str:
    """Helper to read setting default from environment variables (.env)."""
    mapping = {
        "telegram_token": "TELEGRAM_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
        "binance_key": ["BINANCE_API_KEY", "BINANCE_KEY"],
        "binance_secret": "BINANCE_SECRET",
        "http_proxy": ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"],
        "trading_mode": "TRADING_MODE",
        "max_daily_loss": "MAX_DAILY_LOSS",
    }
    target = mapping.get(key)
    candidates = target if isinstance(target, list) else ([target] if isinstance(target, str) else [])
    for t in candidates:
        val = _clean_env_value(os.environ.get(t))
        if val:
            return val
    return ""


def _clean_env_value(val) -> str:
    """Normalise an env value: strip inline comments / quotes and ignore template placeholders."""
    if not val:
        return ""
    s = str(val)
    # systemd EnvironmentFile does not strip inline comments ("paper   # paper | live")
    if " #" in s:
        s = s.split(" #", 1)[0]
    s = s.strip().strip('"').strip("'").strip()
    # Ignore placeholders copied verbatim from .env.example (your_key, your_secret, ...)
    if s.lower().startswith("your_") or s.lower() in {"changeme", "xxx", "none", "null"}:
        return ""
    return s


DEFAULT_ADMIN_PASSWORD = "123456"
INITIAL_PASSWORD_FILE = os.path.join(BASE_DIR, "data", "admin_initial_password.txt")


def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()


def _write_initial_password_file(password: str) -> None:
    """Persist a generated admin password to a 0600 file so the operator can read it once."""
    try:
        fd = os.open(INITIAL_PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(
                "MA Trader 初始管理员账号\n"
                f"username: admin\npassword: {password}\n"
                "首次登录后系统会强制要求修改密码；修改完成后请删除本文件。\n"
            )
    except OSError:
        pass


def init_db() -> None:
    """Create all tables and seed default settings if missing."""
    create_tables()


_TABLES_READY = False


def ensure_tables() -> None:
    """Run create_tables() once per process (cheap guard for hot paths like backtests)."""
    global _TABLES_READY
    if not _TABLES_READY:
        create_tables()


def create_tables() -> None:
    """Create all tables, migrate missing columns, and seed default settings if missing."""
    global _TABLES_READY
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        os.chmod(os.path.dirname(DB_PATH), 0o700)
    except OSError:
        pass
    Base.metadata.create_all(bind=engine)

    # Lightweight schema migration for existing SQLite databases
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(trades)")).fetchall()}
        if "exchange_order_id" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN exchange_order_id VARCHAR"))
        if "sl_order_id" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN sl_order_id VARCHAR"))
        if "trailing_active" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN trailing_active BOOLEAN DEFAULT 0"))
        if "trailing_activated_at" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN trailing_activated_at DATETIME"))
        if "initial_risk" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN initial_risk FLOAT"))
        if "trailing_activate_r" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN trailing_activate_r FLOAT"))
        if "breakeven_buffer" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN breakeven_buffer FLOAT"))
        if "trailing_ma" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN trailing_ma VARCHAR(10)"))
        if "strategy_mode" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN strategy_mode VARCHAR(20)"))
        if "fee" not in cols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN fee FLOAT DEFAULT 0"))

        sym_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(symbol_configs)")).fetchall()}
        if "trailing_activate_r" not in sym_cols:
            conn.execute(text("ALTER TABLE symbol_configs ADD COLUMN trailing_activate_r FLOAT DEFAULT 0.88"))
        if "breakeven_buffer" not in sym_cols:
            conn.execute(text("ALTER TABLE symbol_configs ADD COLUMN breakeven_buffer FLOAT DEFAULT 0.003"))
        if "trailing_ma" not in sym_cols:
            conn.execute(text("ALTER TABLE symbol_configs ADD COLUMN trailing_ma VARCHAR(10) DEFAULT 'ma25'"))

        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        if "must_change_password" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"))

        # Drop the redundant duplicate index (covered by uix_kline_sym_tf_ts)
        conn.execute(text("DROP INDEX IF EXISTS ix_kline_lookup"))
        # Hot query paths: open-position restore, daily-loss check, stats
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trades_status_paper ON trades (status, is_paper)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trades_close_time ON trades (close_time)"))

    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass

    db = SessionLocal()
    try:
        for key, default_val in DEFAULT_SETTINGS.items():
            existing = db.query(SettingModel).filter(SettingModel.key == key).first()
            env_val = _get_env_fallback(key)
            if not existing:
                init_val = env_val if env_val else default_val
                db.add(SettingModel(key=key, value=init_val))
            elif existing.value and existing.value.strip().lower().startswith("your_"):
                # Clean up placeholder values imported from an untouched .env.example
                existing.value = env_val or default_val
            elif not existing.value and env_val:
                # If database setting is empty, populate from .env environment variable
                existing.value = env_val

        # Seed default symbols if empty
        if not db.query(SymbolConfig).first():
            db.add(SymbolConfig(
                symbol="BTCUSDT",
                timeframe="1m",
                htf_timeframe="15m",
                confirm_candles=2,
                leverage=5,
                risk_per_trade=0.01,
                rr_ratio=1.5,
                enabled=True
            ))
            db.add(SymbolConfig(
                symbol="ETHUSDT",
                timeframe="1m",
                htf_timeframe="15m",
                confirm_candles=2,
                leverage=5,
                risk_per_trade=0.01,
                rr_ratio=1.5,
                enabled=True
            ))
        # Seed the admin user. Never ship a well-known default password:
        #   - ADMIN_PASSWORD from env if provided
        #   - otherwise a random password written to data/admin_initial_password.txt (0600)
        # Either way the password must be changed on first login.
        admin_user = db.query(UserModel).filter(UserModel.username == "admin").first()
        if not admin_user:
            env_pwd = _clean_env_value(os.environ.get("ADMIN_PASSWORD"))
            initial_pwd = env_pwd if env_pwd and len(env_pwd) >= 8 else secrets.token_urlsafe(12)
            salt = secrets.token_hex(16)
            db.add(UserModel(
                username="admin",
                password_hash=_pbkdf2(initial_pwd, salt),
                salt=salt,
                must_change_password=True,
            ))
            if not env_pwd:
                _write_initial_password_file(initial_pwd)
                import logging
                logging.getLogger(__name__).warning(
                    "已生成随机初始管理员密码，见 %s（首次登录后需修改）", INITIAL_PASSWORD_FILE
                )
        elif not admin_user.must_change_password:
            # Existing deployments still on the legacy default password are forced to change it
            if hmac.compare_digest(_pbkdf2(DEFAULT_ADMIN_PASSWORD, admin_user.salt), admin_user.password_hash):
                admin_user.must_change_password = True

        db.commit()
    finally:
        db.close()
    _TABLES_READY = True


def get_db():
    """FastAPI dependency — yields a DB session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_paper_equity(db=None, base_capital: float = 10_000.0) -> float:
    """Compute current paper account equity = base_capital + realized net P&L of closed paper trades."""
    from sqlalchemy import func
    own = db is None
    session = SessionLocal() if own else db
    try:
        realized = (
            session.query(func.coalesce(func.sum(Trade.pnl), 0.0))
            .filter(Trade.status == "closed", Trade.is_paper == True)  # noqa: E712
            .scalar()
        )
        return max(100.0, float(base_capital) + float(realized or 0.0))
    except Exception:
        return float(base_capital)
    finally:
        if own:
            session.close()
