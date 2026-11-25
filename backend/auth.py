"""
Authentication utilities for MA Auto Trader:
- Password hashing using PBKDF2-HMAC-SHA256 with salt
- Token-based session management
- Login brute-force protection (in-memory, per client IP and per username)
- FastAPI dependency for route protection
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple, Union

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database import SessionLocal, SessionTokenModel, UserModel, get_db

security = HTTPBearer(auto_error=False)

# In-memory read-through cache backed by SQLite SessionTokenModel
_ACTIVE_TOKENS: Dict[str, dict] = {}
TOKEN_EXPIRE_SECONDS = 7 * 86400  # 7 days

PASSWORD_MIN_LENGTH = 8
WEAK_PASSWORDS = {"123456", "12345678", "123456789", "password", "admin123", "11111111", "88888888", "qwerty123"}

# Paths a user with must_change_password=True may still access
PASSWORD_CHANGE_ALLOWED_PATHS = {"/api/auth/me", "/api/auth/change-password", "/api/auth/logout"}


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


def validate_new_password(new_password: str, old_password: Optional[str] = None) -> Optional[str]:
    """Return an error message if the new password is too weak, else None."""
    if len(new_password) < PASSWORD_MIN_LENGTH:
        return f"新密码长度不能少于 {PASSWORD_MIN_LENGTH} 位"
    if new_password.lower() in WEAK_PASSWORDS:
        return "新密码过于简单，请使用字母、数字组合"
    if old_password is not None and new_password == old_password:
        return "新密码不能与原密码相同"
    if new_password.isdigit() or new_password.isalpha():
        return "新密码需同时包含字母和数字"
    return None


# ---------------------------------------------------------------------------
# Login rate limiting (brute-force protection)
# ---------------------------------------------------------------------------
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILS_PER_IP = 5
LOGIN_MAX_FAILS_PER_USER = 10
_login_fails: Dict[str, Deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()


def _prune(q: Deque[float], now: float) -> None:
    while q and now - q[0] > LOGIN_WINDOW_SECONDS:
        q.popleft()


def login_retry_after(ip: str, username: str) -> int:
    """Return seconds until another login attempt is allowed (0 = allowed)."""
    now = time.time()
    with _login_lock:
        waits = []
        for key, limit in ((f"ip:{ip}", LOGIN_MAX_FAILS_PER_IP), (f"user:{username}", LOGIN_MAX_FAILS_PER_USER)):
            q = _login_fails.get(key)
            if not q:
                continue
            _prune(q, now)
            if len(q) >= limit:
                waits.append(int(LOGIN_WINDOW_SECONDS - (now - q[0])) + 1)
        return max(waits) if waits else 0


def record_login_failure(ip: str, username: str) -> None:
    now = time.time()
    with _login_lock:
        for key in (f"ip:{ip}", f"user:{username}"):
            q = _login_fails[key]
            _prune(q, now)
            q.append(now)


def reset_login_failures(ip: str, username: str) -> None:
    with _login_lock:
        _login_fails.pop(f"ip:{ip}", None)
        _login_fails.pop(f"user:{username}", None)


@dataclass(frozen=True)
class CachedUser:
    id: int
    username: str
    must_change_password: bool


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
def create_token_for_user(user: UserModel) -> str:
    """Issue a new persistent session token for the user."""
    token = secrets.token_hex(32)
    expires_at = time.time() + TOKEN_EXPIRE_SECONDS
    _ACTIVE_TOKENS[token] = {
        "user_id": user.id,
        "username": user.username,
        "must_change_password": bool(getattr(user, "must_change_password", False)),
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


def get_token_from_request(request: Request, allow_query: bool = False) -> Optional[str]:
    """
    Extract token from the Authorization header.

    Query-string tokens (?token=xxx) leak into access logs / proxy logs / browser history,
    so they are only accepted when explicitly allowed (WebSocket handshake).
    """
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip()
    if allow_query:
        return request.query_params.get("token")
    return None


def validate_token(token: Optional[str], db: Optional[Session] = None) -> Optional[Union[UserModel, CachedUser]]:
    """
    Validate token and return user object if valid, else None.
    When *db* is None (hot middleware path), serves valid tokens from the in-memory
    cache with zero SQLite queries. When *db* is provided (e.g. change-password),
    returns the attached SQLAlchemy UserModel.
    """
    if not token:
        return None

    now = time.time()
    cached = _ACTIVE_TOKENS.get(token)
    if cached is not None:
        if now > cached.get("expires_at", 0):
            _ACTIVE_TOKENS.pop(token, None)
            # Fall through to prune expired row from DB
            cached = None
        elif db is None and "must_change_password" in cached:
            return CachedUser(
                id=cached["user_id"],
                username=cached["username"],
                must_change_password=bool(cached["must_change_password"]),
            )

    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        data = cached
        if data is None:
            row = db.query(SessionTokenModel).filter(SessionTokenModel.token == token).first()
            if row is None:
                return None
            if now > row.expires_at:
                db.query(SessionTokenModel).filter(SessionTokenModel.token == token).delete()
                db.commit()
                return None
            data = {
                "user_id": row.user_id,
                "username": row.username,
                "expires_at": row.expires_at,
            }

        user = db.query(UserModel).filter(UserModel.id == data["user_id"]).first()
        if user is None:
            _ACTIVE_TOKENS.pop(token, None)
            return None

        data["must_change_password"] = bool(getattr(user, "must_change_password", False))
        _ACTIVE_TOKENS[token] = data

        if close_db:
            return CachedUser(
                id=user.id,
                username=user.username,
                must_change_password=bool(getattr(user, "must_change_password", False)),
            )
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
    token = credentials.credentials if credentials else get_token_from_request(request)

    user = validate_token(token, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权或登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
