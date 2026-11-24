"""
MA Trader - Statistics Router
Aggregated trade performance metrics.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db, Trade
from models import StatsResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stats"])


@router.get("/stats", response_model=StatsResponse)
def get_stats(
    db: Session = Depends(get_db),
    symbol: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    is_paper: Optional[bool] = Query(None),
):
    """
    Return aggregated trading statistics.

    Query params:
        symbol:   Filter by trading pair (optional).
        days:     Look-back window in calendar days (default 30).
        is_paper: Filter by paper/live mode (optional).
    """
    # Use naive UTC datetime to match SQLite storage
    since = datetime.utcnow() - timedelta(days=days)

    q = db.query(Trade).filter(
        Trade.status == "closed",
        Trade.close_time >= since,
    )
    if symbol and isinstance(symbol, str):
        q = q.filter(Trade.symbol == symbol.upper().strip())
    if is_paper is not None:
        q = q.filter(Trade.is_paper == is_paper)

    trades = q.order_by(Trade.close_time.asc()).all()

    # ---- aggregate ----
    total = len(trades)
    win_trades = [t for t in trades if (t.pnl or 0) > 0]
    loss_trades = [t for t in trades if (t.pnl or 0) <= 0]
    wins = len(win_trades)
    losses = len(loss_trades)
    win_rate = wins / total * 100 if total > 0 else 0.0
    total_pnl = sum(t.pnl or 0 for t in trades)
    total_pnl_pct = sum(t.pnl_pct or 0 for t in trades)

    avg_win = sum(t.pnl or 0 for t in win_trades) / wins if wins else 0.0
    avg_loss = sum(abs(t.pnl or 0) for t in loss_trades) / losses if losses else 0.0

    gross_profit = sum(t.pnl or 0 for t in win_trades)
    gross_loss = sum(abs(t.pnl or 0) for t in loss_trades)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

    # ---- by symbol ----
    by_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        sym = t.symbol
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"] += t.pnl or 0
        if (t.pnl or 0) > 0:
            by_symbol[sym]["wins"] += 1

    by_symbol_resp = {}
    for sym, data in by_symbol.items():
        sym_total = data["trades"]
        by_symbol_resp[sym] = {
            "trades": sym_total,
            "pnl": round(data["pnl"], 4),
            "win_rate": round(data["wins"] / sym_total * 100, 2) if sym_total > 0 else 0.0,
        }

    # ---- daily P&L ----
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.close_time:
            day = t.close_time.strftime("%Y-%m-%d")
            daily[day] += t.pnl or 0

    daily_pnl = [{"date": d, "pnl": round(p, 4)} for d, p in sorted(daily.items())]

    # ---- max drawdown (on cumulative equity curve starting from 10,000 base) ----
    base_eq = 10_000.0
    equity = base_eq
    peak_eq = base_eq
    max_dd_pct = 0.0
    for t in trades:
        equity += (t.pnl or 0.0)
        if equity > peak_eq:
            peak_eq = equity
        if peak_eq > 0:
            dd = (peak_eq - equity) / peak_eq * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd

    return StatsResponse(
        total_trades=total,
        win_trades=wins,
        loss_trades=losses,
        win_rate=round(win_rate, 2),
        total_pnl=round(total_pnl, 4),
        total_pnl_pct=round(total_pnl_pct, 2),
        avg_win=round(avg_win, 4),
        avg_loss=round(avg_loss, 4),
        profit_factor=round(profit_factor, 4),
        max_drawdown=round(max_dd_pct, 2),
        by_symbol=by_symbol_resp,
        daily_pnl=daily_pnl,
        symbol_pnl=[{"symbol": s, **d} for s, d in by_symbol_resp.items()],
    )
