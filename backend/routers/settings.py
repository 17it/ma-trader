"""
API routes for settings and symbol configuration.

Endpoints:
    GET    /api/settings           — return all settings as dict
    PUT    /api/settings           — bulk update settings
    GET    /api/symbols            — list all symbol configs
    POST   /api/symbols            — create a symbol config
    PUT    /api/symbols/{id}       — update a symbol config
    DELETE /api/symbols/{id}       — delete a symbol config
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import SymbolConfig, get_db
from config import get_all_settings, update_settings
from models import (
    SettingResponse,
    SettingUpdate,
    SymbolConfigCreate,
    SymbolConfigResponse,
    SymbolConfigUpdate,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------

@router.get("/settings", response_model=SettingResponse, summary="Get all settings")
def read_settings(db: Session = Depends(get_db)):
    return SettingResponse(settings=get_all_settings(db))


@router.put("/settings", response_model=SettingResponse, summary="Bulk update settings")
def write_settings(body: SettingUpdate, db: Session = Depends(get_db)):
    updated = update_settings(db, body.settings)
    return SettingResponse(settings=updated)


# ---------------------------------------------------------------------------
# Symbol config routes
# ---------------------------------------------------------------------------

@router.get("/symbols", response_model=List[SymbolConfigResponse], summary="List symbol configs")
def list_symbols(db: Session = Depends(get_db)):
    return db.query(SymbolConfig).all()


@router.post(
    "/symbols",
    response_model=SymbolConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create symbol config",
)
def create_symbol(body: SymbolConfigCreate, db: Session = Depends(get_db)):
    existing = db.query(SymbolConfig).filter(SymbolConfig.symbol == body.symbol.upper()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Symbol '{body.symbol}' already exists.",
        )
    obj = SymbolConfig(
        symbol=body.symbol.upper(),
        timeframe=body.timeframe,
        htf_timeframe=body.htf_timeframe,
        confirm_candles=body.confirm_candles,
        leverage=body.leverage,
        risk_per_trade=body.risk_per_trade,
        rr_ratio=body.rr_ratio,
        enabled=body.enabled,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/symbols/{symbol_id}", response_model=SymbolConfigResponse, summary="Update symbol config")
def update_symbol(symbol_id: int, body: SymbolConfigUpdate, db: Session = Depends(get_db)):
    obj = db.query(SymbolConfig).filter(SymbolConfig.id == symbol_id).first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol config not found.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj


@router.delete(
    "/symbols/{symbol_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete symbol config",
)
def delete_symbol(symbol_id: int, db: Session = Depends(get_db)):
    obj = db.query(SymbolConfig).filter(SymbolConfig.id == symbol_id).first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol config not found.")
    db.delete(obj)
    db.commit()
