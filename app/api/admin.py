"""User administration (LLD 5.6; FR-121, FR-132). Admin only (LLD 6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, require_roles
from app.database import get_db
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.schemas.auth import StaffCreateIn, StaffOut, StaffUpdateIn, staff_out
from app.services import accounts

router = APIRouter(prefix="/admin", tags=["administration"])
admin_only = require_roles(UserRole.ADMIN)


@router.get("/users", response_model=list[StaffOut])
def list_users(
    role: UserRole | None = None,
    status: UserStatus | None = None,
    include_applicants: bool = Query(default=False),
    current: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
):
    stmt = select(User).order_by(User.created_at)
    if role is not None:
        stmt = stmt.where(User.role == role)
    elif not include_applicants:
        stmt = stmt.where(User.role != UserRole.APPLICANT)
    if status is not None:
        stmt = stmt.where(User.status == status.value)
    return [staff_out(u) for u in db.scalars(stmt).all()]


@router.post("/users", response_model=StaffOut, status_code=201)
def create_user(
    body: StaffCreateIn, request: Request, current: CurrentUser = Depends(admin_only), db: Session = Depends(get_db)
):
    """Creates a staff account and emails an activation link (FR-121)."""
    user = accounts.create_staff(db, current.user, body.email, body.full_name, body.role, client_ip(request))
    db.commit()
    return staff_out(user)


@router.patch("/users/{user_id}", response_model=StaffOut)
def update_user(
    user_id: int,
    body: StaffUpdateIn,
    request: Request,
    current: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
):
    """Role, status, name or address. Any of them revokes the user's active tokens (LLD 5.6, T-26)."""
    user = accounts.update_staff(
        db, current.user, user_id, body.role, body.status, client_ip(request),
        full_name=body.full_name, email=body.email,
    )  # fmt: skip
    db.commit()
    return staff_out(user)


@router.get("/users/access-review", response_model=list[StaffOut])
def access_review(current: CurrentUser = Depends(admin_only), db: Session = Depends(get_db)):
    """Users, roles, status and last sign-in for periodic access review (FR-132)."""
    rows = db.scalars(select(User).where(User.role != UserRole.APPLICANT).order_by(User.role, User.full_name)).all()
    return [staff_out(u) for u in rows]
