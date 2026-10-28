"""users (LLD 3.1) and authentication operational tables (LLD 3.13)."""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CIText, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import UserRole, UserStatus


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = pk()
    email: Mapped[str] = mapped_column(CIText(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(pg_enum("user_role"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=UserStatus.ACTIVE.value)
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_sign_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Addition to LLD 3.1 (proposed for LLD v1.2): Appendix A requires the applicant email to be
    # verified before submission, which needs a place to record verification.
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    @property
    def is_staff(self) -> bool:
        return self.role != UserRole.APPLICANT


class PasswordResetToken(Base):
    """Also used for staff account activation (staff.account_created, LLD 12)."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class EmailVerificationToken(Base):
    """Addition to LLD 3.13 (proposed for LLD v1.2) — see User.email_verified_at."""

    __tablename__ = "email_verification_tokens"

    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
