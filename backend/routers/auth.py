"""
Authentication Router for MA Auto Trader:
- /api/auth/login
- /api/auth/me
- /api/auth/change-password
- /api/auth/logout
"""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import INITIAL_PASSWORD_FILE, UserModel, get_db
from auth import (
    PASSWORD_MIN_LENGTH,
    create_token_for_user,
    get_current_user,
    get_token_from_request,
    hash_password,
    login_retry_after,
    record_login_failure,
    reset_login_failures,
    revoke_all_tokens_for_user,
    revoke_token,
    validate_new_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    # Only trust direct peer IP by default (avoid X-Forwarded-For spoofing bypass)
    return request.client.host if request.client else "unknown"


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=128)


@router.post("/login", summary="用户登录")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = _client_ip(request)
    username = body.username.strip()

    retry_after = login_retry_after(ip, username)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    user = db.query(UserModel).filter(UserModel.username == username).first()
    if not user or not verify_password(body.password, user.password_hash, user.salt):
        record_login_failure(ip, username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    reset_login_failures(ip, username)
    token = create_token_for_user(user)
    return {
        "success": True,
        "token": token,
        "username": user.username,
        "must_change_password": bool(user.must_change_password),
        "message": "登录成功",
    }


@router.get("/me", summary="获取当前登录用户信息")
def get_me(user: UserModel = Depends(get_current_user)):
    return {
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
        "must_change_password": bool(user.must_change_password),
    }


@router.post("/change-password", summary="修改当前用户密码")
def change_password(
    body: ChangePasswordRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.old_password, user.password_hash, user.salt):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码错误，请重新输入",
        )

    pwd_err = validate_new_password(body.new_password, old_password=body.old_password)
    if pwd_err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pwd_err)

    new_hash, new_salt = hash_password(body.new_password)
    user.password_hash = new_hash
    user.salt = new_salt
    user.must_change_password = False
    db.commit()

    # Remove one-time initial password file once the password has been rotated
    try:
        if os.path.exists(INITIAL_PASSWORD_FILE):
            os.remove(INITIAL_PASSWORD_FILE)
    except OSError:
        pass

    revoke_all_tokens_for_user(user.id)
    new_token = create_token_for_user(user)

    return {
        "success": True,
        "token": new_token,
        "must_change_password": False,
        "message": "密码修改成功！新密码已生效。",
    }


@router.post("/logout", summary="退出登录")
def logout(request: Request, user: UserModel = Depends(get_current_user)):
    token = get_token_from_request(request)
    if token:
        revoke_token(token)
    return {"success": True, "message": "已成功退出登录"}
