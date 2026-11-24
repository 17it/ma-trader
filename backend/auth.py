"""
Authentication utilities for MA Auto Trader:
- Password hashing using PBKDF2-HMAC-SHA256 with salt
- Token-based session management
- FastAPI dependency for route protection
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database import SessionLocal, SessionTokenModel, UserModel, get_db

security = HTTPBearer(auto_error=False)

# In-memory read-through cache backed by SQLite SessionTokenModel
_ACTIVE_TOKENS: Dict[str, dict] = {}
TOKEN_EXPIRE_SECONDS = 30 * 86400  # 30 days


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Generate (hash, salt) for password using PBKDF2-HMAC-SHA256."""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,
    ).hex()
    return hashed, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Verify plain password against stored hash and salt."""
    expected_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(expected_hash, password_hash)


def create_token_for_user(user: UserModel) -> str:
    """Issue a new persistent session token for the user."""
    token = secrets.token_hex(32)
    expires_at = time.time() + TOKEN_EXPIRE_SECONDS
    _ACTIVE_TOKENS[token] = {
        "user_id": user.id,
        "username": user.username,
        "expires_at": expires_at,
    }
    db = SessionLocal()
    try:
        db.add(SessionTokenModel(
            token=token,
            user_id=user.id,
            username=user.username,
            expires_at=expires_at,
        ))
        # Prune expired tokens opportunistically
        db.query(SessionTokenModel).filter(SessionTokenModel.expires_at < time.time()).delete()
        db.commit()
    finally:
        db.close()
    return token


def revoke_token(token: str) -> None:
    """Revoke an active token."""
    _ACTIVE_TOKENS.pop(token, None)
    db = SessionLocal()
    try:
        db.query(SessionTokenModel).filter(SessionTokenModel.token == token).delete()
        db.commit()
    finally:
        db.close()


def revoke_all_tokens_for_user(user_id: int) -> None:
    """Revoke all tokens for a specific user (e.g. after password change)."""
    to_delete = [
        t for t, data in _ACTIVE_TOKENS.items() if data.get("user_id") == user_id
    ]
    for t in to_delete:
        _ACTIVE_TOKENS.pop(t, None)
    db = SessionLocal()
    try:
        db.query(SessionTokenModel).filter(SessionTokenModel.user_id == user_id).delete()
        db.commit()
    finally:
        db.close()


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from Authorization header or query param."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[len("Bearer ") :].strip()
    # Fallback to query param (?token=xxx)
    return request.query_params.get("token")


def validate_token(token: Optional[str], db: Optional[Session] = None) -> Optional[UserModel]:
    """Validate token and return the UserModel if valid, else None."""
    if not token:
        return None

    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        data = _ACTIVE_TOKENS.get(token)
        if data is None:
            row = db.query(SessionTokenModel).filter(SessionTokenModel.token == token).first()
            if row is None:
                return None
            data = {
                "user_id": row.user_id,
                "username": row.username,
                "expires_at": row.expires_at,
            }
            _ACTIVE_TOKENS[token] = data

        if time.time() > data.get("expires_at", 0):
            _ACTIVE_TOKENS.pop(token, None)
            db.query(SessionTokenModel).filter(SessionTokenModel.token == token).delete()
            db.commit()
            return None

        user = db.query(UserModel).filter(UserModel.id == data["user_id"]).first()
        return user
    finally:
        if close_db:
            db.close()


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> UserModel:
    """FastAPI dependency to enforce authentication on protected endpoints."""
    token = None
    if credentials:
        token = credentials.credentials
    else:
        token = get_token_from_request(request)

    user = validate_token(token, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权或登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
