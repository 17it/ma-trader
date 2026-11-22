"""
Pydantic request / response schemas for MA Auto Trader.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingUpdate(BaseModel):
    """Body for PUT /api/settings — a free-form key/value map."""
    settings: Dict[str, str] = Field(
        ...,
        example={
            "trading_mode": "paper",
            "max_daily_loss": "0.05",
        },
    )


class SettingResponse(BaseModel):
    """Response for GET /api/settings."""
    settings: Dict[str, str]


# ---------------------------------------------------------------------------
# Symbol Config
# ---------------------------------------------------------------------------

class SymbolConfigCreate(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    timeframe: str = Field("1m", example="1m")
    htf_timeframe: str = Field("15m", example="15m")
    confirm_candles: int = Field(2, ge=1, le=20)
    leverage: int = Field(5, ge=1, le=125)
    risk_per_trade: float = Field(0.01, gt=0, le=1)
    rr_ratio: float = Field(1.5, gt=0)
    enabled: bool = True


class SymbolConfigUpdate(BaseModel):
    timeframe: Optional[str] = None
    htf_timeframe: Optional[str] = None
    confirm_candles: Optional[int] = Field(None, ge=1, le=20)
    leverage: Optional[int] = Field(None, ge=1, le=125)
    risk_per_trade: Optional[float] = Field(None, gt=0, le=1)
    rr_ratio: Optional[float] = Field(None, gt=0)
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


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class BacktestRunCreate(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    timeframe: str = Field("1m", example="1m")
    start_date: str = Field(..., example="2024-01-01")
    end_date: str = Field(..., example="2024-12-31")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        example={
            "confirm_candles": 2,
            "rr_ratio": 1.5,
            "initial_capital": 10000,
        },
    )


class BacktestRunResponse(BaseModel):
    id: int
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
    total_trades: int
    win_rate: float                    # 0.0 – 1.0
    total_pnl: float
    total_pnl_pct: float
    profit_factor: float
    max_drawdown: float
    avg_win: float
    avg_loss: float
    best_symbol: Optional[str] = None
    daily_pnl: List[Dict[str, Any]] = Field(default_factory=list)
    symbol_pnl: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Bot Status
# ---------------------------------------------------------------------------

class TradingStatus(BaseModel):
    running: bool
    mode: str                          # "paper" or "live"
    active_symbols: List[str] = Field(default_factory=list)
    open_trades: int = 0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    uptime_seconds: int = 0
