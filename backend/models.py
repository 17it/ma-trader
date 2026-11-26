"""
Pydantic request / response schemas for MA Auto Trader.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

VALID_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
TimeframeType = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}(/[A-Z0-9]{2,10})?$")


def validate_symbol_str(v: str) -> str:
    s = (v or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise ValueError("交易对格式无效，仅允许字母与数字（例如 BTCUSDT）")
    return s


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingUpdate(BaseModel):
    """Body for PUT /api/settings — a free-form key/value map."""
    settings: Dict[str, str] = Field(
        ...,
        example={
            "trading_mode": "paper",
            "max_daily_loss": "5",
        },
    )


class SettingResponse(BaseModel):
    """Response for GET /api/settings."""
    settings: Dict[str, str]


# ---------------------------------------------------------------------------
# Symbol Config
# ---------------------------------------------------------------------------

class SymbolConfigCreate(BaseModel):
    symbol: str = Field(..., min_length=4, max_length=25, example="BTCUSDT")
    timeframe: TimeframeType = Field("1m", example="1m")
    htf_timeframe: TimeframeType = Field("15m", example="15m")
    confirm_candles: int = Field(2, ge=1, le=20)
    leverage: int = Field(5, ge=1, le=20)
    risk_per_trade: float = Field(0.01, gt=0, le=0.2)
    rr_ratio: float = Field(1.5, ge=0.5, le=20.0)
    trailing_activate_r: float = Field(0.88, ge=0.1, le=10.0)
    breakeven_buffer: float = Field(0.003, ge=0.0, le=0.05)
    enabled: bool = True

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        return validate_symbol_str(v)


class SymbolConfigUpdate(BaseModel):
    timeframe: Optional[TimeframeType] = None
    htf_timeframe: Optional[TimeframeType] = None
    confirm_candles: Optional[int] = Field(None, ge=1, le=20)
    leverage: Optional[int] = Field(None, ge=1, le=20)
    risk_per_trade: Optional[float] = Field(None, gt=0, le=0.2)
    rr_ratio: Optional[float] = Field(None, ge=0.5, le=20.0)
    trailing_activate_r: Optional[float] = Field(None, ge=0.1, le=10.0)
    breakeven_buffer: Optional[float] = Field(None, ge=0.0, le=0.05)
    enabled: Optional[bool] = None


class SymbolConfigResponse(BaseModel):
    id: int
    symbol: str
    timeframe: str
    htf_timeframe: str
    confirm_candles: int
    leverage: int
    risk_per_trade: float
    rr_ratio: float
    trailing_activate_r: float = 0.88
    breakeven_buffer: float = 0.003
    enabled: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class TradeResponse(BaseModel):
    id: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: Optional[float] = None
    stop_loss: float
    take_profit: float
    quantity: float
    status: str
    close_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    is_paper: bool
    open_time: datetime
    close_time: Optional[datetime] = None
    timeframe: str
    confirm_count: int
    ma7_at_entry: Optional[float] = None
    ma25_at_entry: Optional[float] = None
    ma60_at_entry: Optional[float] = None

    model_config = {"from_attributes": True}


class ManualCloseRequest(BaseModel):
    exit_price: Optional[float] = Field(None, gt=0)


class SimulateTradeRequest(BaseModel):
    symbol: str = Field(..., min_length=4, max_length=25)
    direction: Literal["long", "short"] = "long"
    entry_price: float = Field(..., gt=0)

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        return validate_symbol_str(v)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class BacktestRunCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=120, example="5m/15m趋势追踪测试")
    symbol: str = Field(..., min_length=4, max_length=25, example="BTCUSDT")
    timeframe: TimeframeType = Field("1m", example="1m")
    htf_timeframe: TimeframeType = Field("15m", example="15m")
    start_date: str = Field(..., example="2024-01-01")
    end_date: str = Field(..., example="2024-12-31")
    confirm_candles: int = Field(2, ge=1, le=20)
    rr_ratio: float = Field(1.5, ge=0.5, le=20.0)
    trailing_activate_r: float = Field(0.88, ge=0.1, le=10.0)
    breakeven_buffer: float = Field(0.003, ge=0.0, le=0.05)
    initial_capital: float = Field(10000.0, gt=0, le=1e9)
    risk_per_trade: float = Field(0.01, gt=0, le=0.2)
    leverage: int = Field(5, ge=1, le=20)
    fee_rate: float = Field(0.0004, ge=0.0, le=0.01)
    slippage_rate: float = Field(0.0001, ge=0.0, le=0.01)
    params: Dict[str, Any] = Field(
        default_factory=dict,
        example={
            "confirm_candles": 2,
            "rr_ratio": 1.5,
            "initial_capital": 10000,
        },
    )

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        return validate_symbol_str(v)


class BacktestRunResponse(BaseModel):
    id: int
    title: Optional[str] = None
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    params: str                        # stored as JSON string in DB
    results: Optional[str] = None      # JSON string
    status: str
    created_at: datetime
    error_msg: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class StatsResponse(BaseModel):
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_symbol: Optional[str] = None
    by_symbol: Dict[str, Any] = Field(default_factory=dict)
    daily_pnl: List[Dict[str, Any]] = Field(default_factory=list)
    symbol_pnl: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Bot Status
# ---------------------------------------------------------------------------

class TradingStatus(BaseModel):
    running: bool
    paper_running: bool = False
    live_running: bool = False
    mode: str                          # "paper" or "live" or "both"
    active_symbols: List[str] = Field(default_factory=list)
    open_trades: int = 0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    uptime_seconds: int = 0


# ---------------------------------------------------------------------------
# Operation Logs
# ---------------------------------------------------------------------------

class OperationLogResponse(BaseModel):
    id: int
    log_type: str
    level: str
    symbol: Optional[str] = None
    mode: Optional[str] = None
    title: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OperationLogPageResponse(BaseModel):
    items: List[OperationLogResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 1


# ---------------------------------------------------------------------------
# System Logs
# ---------------------------------------------------------------------------

class SystemLogResponse(BaseModel):
    id: int
    level: str
    symbol: Optional[str] = None
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SystemLogPageResponse(BaseModel):
    items: List[SystemLogResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    total_pages: int = 1
