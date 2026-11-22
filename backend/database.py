"""
SQLAlchemy models and database initialization for MA Auto Trader.
Database: SQLite at data/trading.db
"""

import os
from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Index, UniqueConstraint, text,
    create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------------------------------------------------------------------------
# Engine setup
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "trading.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class SettingModel(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String)


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


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String)
    timeframe = Column(String)
    start_date = Column(String)
    end_date = Column(String)
    params = Column(String)                                    # JSON string
    results = Column(String, nullable=True)                    # JSON string
    status = Column(String, default="pending")                 # "pending","running","done","failed"
    created_at = Column(DateTime, default=datetime.utcnow)
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

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uix_kline_sym_tf_ts"),
        Index("ix_kline_lookup", "symbol", "timeframe", "open_time"),
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
    "max_daily_loss": "0.05",  # 5%
}


def init_db() -> None:
    """Create all tables and seed default settings if missing."""
    create_tables()


def create_tables() -> None:
    """Create all tables and seed default settings if missing."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        for key, value in DEFAULT_SETTINGS.items():
            existing = db.query(SettingModel).filter(SettingModel.key == key).first()
            if not existing:
                db.add(SettingModel(key=key, value=value))

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
        db.commit()
    finally:
        db.close()


def get_db():
    """FastAPI dependency — yields a DB session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
