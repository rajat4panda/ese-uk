"""audit_entries (LLD 3.10). Append-only: app_role holds INSERT and SELECT only (NFR-12)."""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, pk


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[int] = pk()
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    user_role: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT")
    )
    # FK to student_records(id) is added by the migration that creates student_records (Sprint 4).
    student_id: Mapped[int | None] = mapped_column(BigInteger)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    ip_address: Mapped[str | None] = mapped_column(INET)
    # Server clock only; never client-supplied. clock_timestamp() rather than now() so entries
    # written in one transaction keep their true order.
    at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=text("clock_timestamp()"))
