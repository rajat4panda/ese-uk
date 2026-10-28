"""Authentication and administration request/response schemas (LLD 5.1, 5.6)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.config import get_settings
from app.models.enums import UserRole, UserStatus


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_In):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    full_name: str = Field(max_length=400)


class LoginIn(_In):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class ResetRequestIn(_In):
    email: str = Field(max_length=320)


class ResetConfirmIn(_In):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(max_length=1024)


class VerifyEmailIn(_In):
    token: str = Field(min_length=10, max_length=200)


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: UserRole
    email_verified: bool


class TokenOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class StaffCreateIn(_In):
    email: str = Field(max_length=320)
    full_name: str = Field(max_length=400)
    role: UserRole


class StaffUpdateIn(_In):
    role: UserRole | None = None
    status: UserStatus | None = None
    # FR-100: a misspelled name or address is a correction, not a new account. Changing either
    # leaves the account and its history intact.
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    email: EmailStr | None = None


class StaffOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: UserRole
    status: str
    last_sign_in_at: datetime | None
    created_at: datetime


def user_out(u) -> UserOut:  # noqa: ANN001
    return UserOut(id=u.id, name=u.full_name, email=u.email, role=u.role,
                   email_verified=u.email_verified_at is not None
                   or not get_settings().email_verification_required)  # fmt: skip


def staff_out(u) -> StaffOut:  # noqa: ANN001
    return StaffOut(id=u.id, email=u.email, full_name=u.full_name, role=u.role, status=u.status,
                    last_sign_in_at=u.last_sign_in_at, created_at=u.created_at)  # fmt: skip
