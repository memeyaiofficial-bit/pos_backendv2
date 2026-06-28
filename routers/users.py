"""
routers/users.py
─────────────────
Admin-only endpoints for managing staff accounts.

ACCESS CONTROL:
  • All endpoints require ADMIN role except GET /users/me (own profile).
  • Admins cannot delete themselves (safety guard).
  • Role changes are audit-logged.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models.orm import AuditLog, User, UserRole
from schemas.schemas import UserCreateIn, UserOut, UserUpdateIn
from utils.security import (
    get_current_user, hash_password,
    require_admin, require_roles,
)

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=list[UserOut], summary="List all staff (admin)")
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    role: UserRole = Query(None),
    is_active: bool = Query(None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return all staff accounts with optional filtering."""
    q = db.query(User)
    if role is not None:
        q = q.filter(User.role == role)
    if is_active is not None:
        q = q.filter(User.is_active == is_active)
    return q.order_by(User.full_name).offset(skip).limit(limit).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="Create staff account (admin)")
def create_user(
    payload: UserCreateIn,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new staff account. Email must be unique."""
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email '{payload.email}' already exists",
        )

    user = User(
        full_name=payload.full_name,
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
        phone=payload.phone,
    )
    db.add(user)
    db.flush()

    db.add(AuditLog(
        user_id=current_admin.id,
        action="USER_CREATED",
        entity="User",
        entity_id=user.id,
        detail=f"Admin created user {user.email} with role={user.role}",
    ))
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut, summary="Get staff account (admin)")
def get_user(
    user_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.patch("/{user_id}", response_model=UserOut, summary="Update staff account (admin)")
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update name, phone, role or active status. Partial updates supported."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(user, field, value)

    db.add(AuditLog(
        user_id=current_admin.id,
        action="USER_UPDATED",
        entity="User",
        entity_id=user.id,
        detail=f"Updated fields: {list(changes.keys())}",
    ))
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Deactivate staff account (admin)")
def deactivate_user(
    user_id: int,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Soft-delete: deactivates the account (preserves audit trail).
    Admins cannot deactivate themselves.
    """
    if user_id == current_admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account",
        )

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.is_active = False
    db.add(AuditLog(
        user_id=current_admin.id,
        action="USER_DEACTIVATED",
        entity="User",
        entity_id=user.id,
        detail=f"Admin deactivated user {user.email}",
    ))
    db.commit()


@router.post("/{user_id}/reset-password", summary="Reset another user's password (admin)")
def reset_password(
    user_id: int,
    new_password: str,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin can reset any user's password (temp password to share via secure channel)."""
    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters",
        )

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.hashed_password = hash_password(new_password)
    db.add(AuditLog(
        user_id=current_admin.id,
        action="PASSWORD_RESET",
        entity="User",
        entity_id=user.id,
        detail=f"Admin {current_admin.email} reset password for {user.email}",
    ))
    db.commit()
    return {"message": f"Password reset for {user.email}"}