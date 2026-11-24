"""
Authentication Router for MA Auto Trader:
- /api/auth/login
- /api/auth/me
- /api/auth/change-password
- /api/auth/logout
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import UserModel, get_db
from auth import (
    create_token_for_user,
    get_current_user,
    hash_password,
    revoke_all_tokens_for_user,
    revoke_token,
    verify_password,
    get_token_from_request,
)
from fastapi import Request

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=100)


@router.post("/login", summary="用户登录")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserModel).filter(UserModel.username == body.username.strip()).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    if not verify_password(body.password, user.password_hash, user.salt):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_token_for_user(user)
    return {
        "success": True,
        "token": token,
        "username": user.username,
        "message": "登录成功",
    }


@router.get("/me", summary="获取当前登录用户信息")
def get_me(user: UserModel = Depends(get_current_user)):
    return {
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
    }


@router.post("/change-password", summary="修改当前用户密码")
def change_password(
    body: ChangePasswordRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Verify old password
    if not verify_password(body.old_password, user.password_hash, user.salt):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码错误，请重新输入",
        )

    if len(body.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码长度不能少于6位",
        )

    # Hash new password with fresh salt
    new_hash, new_salt = hash_password(body.new_password)
    user.password_hash = new_hash
    user.salt = new_salt
    db.commit()

    # Revoke old tokens and issue new one
    revoke_all_tokens_for_user(user.id)
    new_token = create_token_for_user(user)

    return {
        "success": True,
        "token": new_token,
        "message": "密码修改成功！请使用新密码登录或继续使用。",
    }


@router.post("/logout", summary="退出登录")
def logout(request: Request, user: UserModel = Depends(get_current_user)):
    token = get_token_from_request(request)
    if token:
        revoke_token(token)
    return {"success": True, "message": "已成功退出登录"}
