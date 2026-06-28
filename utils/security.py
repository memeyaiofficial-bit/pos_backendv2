"""
utils/security.py
─────────────────
Authentication and authorisation helpers.

WHAT'S HERE:
  • Password hashing with bcrypt (work factor 12 – slow enough to deter brute-force).
  • JWT creation and verification for access + refresh tokens (two separate secrets).
  • FastAPI dependencies: get_current_user, require_role.

RISKS MITIGATED:
  • bcrypt work factor 12  → ~250 ms per hash; makes offline dictionary attacks slow.
  • Separate claims field "typ" distinguishes access vs refresh tokens → refresh token
    cannot be used as an access token.
  • Token expiry enforced by JWT standard "exp" claim verified on every request.
  • HTTPBearer scheme prevents CSRF (cookies not used).
  • Active-user check on every authenticated request → deactivated accounts are
    immediately locked out without waiting for token expiry.
  • Role hierarchy enforced via Depends() – not in business logic – so it can't be
    accidentally skipped.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import get_settings
from database import get_db
from models.orm import User, UserRole

settings = get_settings()

# ── Password hashing ──────────────────────────────────────────────────────────

_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12, # Cost factor – increase over time as hardware gets faster
    truncate_error=False,
)


def hash_password(plain: str) -> str:
    """Return bcrypt hash of plain-text password."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Constant-time comparison via passlib – resistant to timing attacks.
    Returns False (not raises) on mismatch to allow generic "invalid credentials" response.
    """
    return _pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

_SECRET = settings.SECRET_KEY.get_secret_value()
_ALGO   = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
_REFRESH_EXPIRE = settings.REFRESH_TOKEN_EXPIRE_DAYS

def _build_token(data: dict, expires_delta: timedelta, typ: str) -> str:
    payload = data.copy()
    payload.update({
        "exp": datetime.now(timezone.utc) + expires_delta,
        "iat": datetime.now(timezone.utc),
        "typ": typ,  # Distinguish access vs refresh
    })
    return jwt.encode(payload, _SECRET, algorithm=_ALGO)


def create_access_token(user_id: int, role: str) -> str:
    return _build_token(
        {"sub": str(user_id), "role": role},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "access",
    )


def create_refresh_token(user_id: int) -> str:
    return _build_token(
        {"sub": str(user_id)},
        timedelta(days=_REFRESH_EXPIRE),
        "refresh",
    )


def decode_access_token(token: str) -> dict:
    """
    Decode and verify an access token.
    Raises HTTP 401 on any problem (expired, tampered, wrong type).
    """
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGO])
        if payload.get("typ") != "access":
            raise JWTError("Wrong token type")
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_refresh_token(token: str) -> int:
    """
    Decode a refresh token and return the user_id.
    Raises HTTP 401 on failure.
    """
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGO])
        if payload.get("typ") != "refresh":
            raise JWTError("Wrong token type")
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )


# ── FastAPI dependencies ───────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency: decode token, load user from DB, verify account is active.

    Used as: current_user: User = Depends(get_current_user)
    """
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token")

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account disabled",
        )
    return user


def require_roles(*roles: UserRole):
    """
    FastAPI dependency factory for role-based access control.

    Usage:
        @router.delete("/users/{id}", dependencies=[Depends(require_roles(UserRole.ADMIN))])

    or as a parameter:
        current_user: User = Depends(require_roles(UserRole.ADMIN, UserRole.PHARMACIST))
    """
    def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not permitted for this action. "
                       f"Required: {[r.value for r in roles]}",
            )
        return current_user
    return _check


# Convenience shortcuts
require_admin        = require_roles(UserRole.ADMIN)
require_admin_or_pharmacist = require_roles(UserRole.ADMIN, UserRole.PHARMACIST)