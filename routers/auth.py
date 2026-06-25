"""
routers/auth.py
───────────────
Authentication endpoints: login, token refresh, password change.

ENDPOINTS:
  POST /auth/login          → returns access + refresh token pair
  POST /auth/refresh        → exchange refresh token for new access token
  POST /auth/change-password → change own password (authenticated)
  GET  /auth/me             → return current user profile

SECURITY NOTES:
  • Failed logins return a generic message (no "email not found" vs "wrong password"
    distinction) to prevent user enumeration.
  • last_login timestamp updated on every successful login for audit.
  • Password change requires current password verification.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models.orm import AuditLog, User
from schemas.schemas import (
    LoginIn, PasswordChangeIn, TokenOut, TokenRefreshIn, UserOut, UpdateCredentialsIn,
)
from utils.security import (
    create_access_token, create_refresh_token, decode_refresh_token,
    get_current_user, hash_password, verify_password,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request

limiter = Limiter(key_func=get_remote_address)

from config import get_settings
from pydantic import EmailStr

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenOut, summary="Login with email + password")
@limiter.limit("5/minute")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    """
    Authenticate a staff member.  Returns JWT access + refresh tokens.

    The access token is short-lived (30 min default).
    The refresh token is long-lived (7 days default) and used only to
    obtain new access tokens – never for data requests.
    """
    # Generic error for both "not found" and "wrong password" – prevents enumeration
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user: User = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user:
        raise invalid_credentials

    if not verify_password(payload.password, user.hashed_password):
        # Log failed attempt for monitoring
        db.add(AuditLog(
            user_id=None,
            action="LOGIN_FAILED",
            detail=f"Failed login attempt for email={payload.email}",
            ip_address=request.client.host if request.client else None,
        ))
        db.commit()
        raise invalid_credentials

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Contact your administrator.",
        )

    # Update last_login timestamp
    user.last_login = datetime.now(timezone.utc)

    # Audit successful login
    db.add(AuditLog(
        user_id=user.id,
        action="LOGIN_SUCCESS",
        detail=f"User {user.email} logged in",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    return TokenOut(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        must_change_credentials=user.must_change_credentials,
    )


@router.post("/refresh", response_model=TokenOut, summary="Refresh access token")
def refresh_token(payload: TokenRefreshIn, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access token + refresh token pair.
    Rotating refresh tokens limits the window of token theft exploitation.
    """
    user_id = decode_refresh_token(payload.refresh_token)
    user = db.get(User, user_id)

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account disabled",
        )

    return TokenOut(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        must_change_credentials=user.must_change_credentials,
    )



@router.get("/me", response_model=UserOut, summary="Get current user profile")
def me(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return current_user


@router.post("/change-password", summary="Change own password")
def change_password(
    payload: PasswordChangeIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allow a user to change their own password.
    Requires correct current password to prevent session-hijack exploitation.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from current password",
        )

    current_user.hashed_password = hash_password(payload.new_password)

    db.add(AuditLog(
        user_id=current_user.id,
        action="PASSWORD_CHANGED",
        entity="User",
        entity_id=current_user.id,
        detail="User changed their own password",
    ))
    db.commit()

    return {"message": "Password changed successfully"}

@router.post("/update-credentials")
def update_credentials(
    payload: UpdateCredentialsIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Forces first-time admin to replace default email/password.
    """

    if not verify_password(
        payload.current_password,
        current_user.hashed_password
    ):
        raise HTTPException(
            status_code=400,
            detail="Current password is incorrect"
        )

    # Check email uniqueness
    existing_user = (
        db.query(User)
        .filter(
            User.email == payload.email.lower(),
            User.id != current_user.id
        )
        .first()
    )

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Email already in use"
        )

    current_user.email = payload.email.lower()
    current_user.hashed_password = hash_password(
        payload.new_password
    )

    current_user.must_change_credentials = False

    db.commit()

    return {
        "message": "Credentials updated successfully"
    }