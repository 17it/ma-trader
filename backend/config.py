"""
Configuration management — read/write the 'settings' table.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Dict, Optional
from sqlalchemy.orm import Session

sys.modules.setdefault("config", sys.modules[__name__])
sys.modules.setdefault("backend.config", sys.modules[__name__])

from database import SettingModel, DEFAULT_SETTINGS, SessionLocal


class ConfigManager:
    """
    Thin wrapper used by main.py via app.state.config.
    Caches settings in memory with a short TTL and explicit invalidation on write.
    """

    def __init__(self, ttl_seconds: float = 30.0):
        self._ttl = ttl_seconds
        self._cache: Optional[Dict[str, str]] = None
        self._cache_ts: float = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_ts = 0.0

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        data = self.get_all_cached()
        val = data.get(key)
        return default if val is None else val

    def set(self, key: str, value: str) -> None:
        db = SessionLocal()
        try:
            set_setting(db, key, value)
        finally:
            db.close()
        self.invalidate()

    def all(self) -> Dict[str, str]:
        db = SessionLocal()
        try:
            data = get_all_settings(db)
        finally:
            db.close()
        with self._lock:
            self._cache = dict(data)
            self._cache_ts = time.monotonic()
        return data

    def get_all(self, db: Optional[Session] = None) -> Dict[str, str]:
        """Return all settings using an injected session if given, else the cached copy."""
        if db is not None:
            data = get_all_settings(db)
            with self._lock:
                self._cache = dict(data)
                self._cache_ts = time.monotonic()
            return data
        return self.get_all_cached()

    def get_all_cached(self) -> Dict[str, str]:
        """Return cached settings dict, refreshing from SQLite when TTL expires."""
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and (now - self._cache_ts) < self._ttl:
                return dict(self._cache)
        return self.all()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_all_settings(db: Session) -> Dict[str, str]:
    """Return all settings as a plain dict."""
    rows = db.query(SettingModel).all()
    return {row.key: (row.value or "") for row in rows}


def get_setting(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a single setting value, or *default* if not found."""
    row = db.query(SettingModel).filter(SettingModel.key == key).first()
    if row is None:
        return default
    return row.value


def update_settings(db: Session, updates: Dict[str, str]) -> Dict[str, str]:
    """
    Upsert multiple settings at once.
    Returns the full settings dict after the update.
    """
    for key, value in updates.items():
        row = db.query(SettingModel).filter(SettingModel.key == key).first()
        if row:
            row.value = value
        else:
            db.add(SettingModel(key=key, value=value))
    db.commit()
    return get_all_settings(db)


def set_setting(db: Session, key: str, value: str) -> None:
    """Upsert a single setting."""
    update_settings(db, {key: value})


def reset_to_defaults(db: Session) -> Dict[str, str]:
    """Re-seed all default settings (only fills blanks, does not overwrite)."""
    for key, value in DEFAULT_SETTINGS.items():
        existing = db.query(SettingModel).filter(SettingModel.key == key).first()
        if not existing:
            db.add(SettingModel(key=key, value=value))
    db.commit()
    return get_all_settings(db)


# ---------------------------------------------------------------------------
# Typed accessors for frequently-used settings
# ---------------------------------------------------------------------------

def get_trading_mode(db: Session) -> str:
    return get_setting(db, "trading_mode", "paper") or "paper"


def get_max_daily_loss(db: Session) -> float:
    """Return max daily loss percentage (e.g. 5.0 means 5%)."""
    value = get_setting(db, "max_daily_loss", "5")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0


def get_binance_credentials(db: Session) -> Dict[str, str]:
    return {
        "api_key": get_setting(db, "binance_key", "") or "",
        "secret": get_setting(db, "binance_secret", "") or "",
    }


def get_telegram_config(db: Session) -> Dict[str, str]:
    return {
        "token": get_setting(db, "telegram_token", "") or "",
        "chat_id": get_setting(db, "telegram_chat_id", "") or "",
    }
