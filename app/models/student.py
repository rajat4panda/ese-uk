"""student_records (LLD 3.7). Created as an effect of CAS assignment, before any visa decision.

Attendance, engagement and reportable events arrive in Sprint 4; the record and its lifecycle
statuses exist now because the CAS transition writes one (LLD 7.3, 7.4).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import EnrolmentStatus


class StudentRecord(Base):
    __tablename__ = "student_records"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    cas_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cas.id", ondelete="RESTRICT"), nullable=False)
    enrolment_status: Mapped[EnrolmentStatus] = mapped_column(
        pg_enum("enrolment_status"), nullable=False, server_default=EnrolmentStatus.AWAITING_ARRIVAL.value
    )
    # Addition to LLD 3.7 (proposed for v1.2): LLD 7.4 requires the reason a record was closed
    # (visa_refused, cas_withdrawn, non_enrolment) but gives it no column.
    enrolment_status_reason: Mapped[str | None] = mapped_column(Text)
    enrolment_date: Mapped[date | None] = mapped_column(Date)
    enrolment_evidence_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT")
    )
    permission_expiry: Mapped[date | None] = mapped_column(Date)
    last_status_check: Mapped[date | None] = mapped_column(Date)
    completion_outcome: Mapped[str | None] = mapped_column(Text)
    #: FR-78. When the current attendance breach was alerted; cleared the moment the student is
    #: back above the threshold, so the next breach is treated as a new one.
    engagement_alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completion_date: Mapped[date | None] = mapped_column(Date)
    contact_phone: Mapped[str | None] = mapped_column(Text)  # FR-67, current
    contact_email: Mapped[str | None] = mapped_column(Text)
    current_address: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())

    cas = relationship("Cas", lazy="joined")


class StudentNote(Base):
    """A case note (prototype screen 12, Notes tab). Append-only: the application role may insert
    and read, never update or delete (migration 0009), so a note stands as written."""

    __tablename__ = "student_notes"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"),
                                            nullable=False)  # fmt: skip
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )


class StatusCheck(Base):
    """A right-to-study / immigration status check (BRD FR-68). Append-only (migration 0010)."""

    __tablename__ = "status_checks"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"),
                                            nullable=False)  # fmt: skip
    check_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str | None] = mapped_column(Text)
    permission_type: Mapped[str | None] = mapped_column(Text)
    permission_expiry: Mapped[date | None] = mapped_column(Date)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT")
    )
    checked_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )


class ContactChange(Base):
    """A change of address or contact details with the date it took effect (BRD FR-67). Append-only."""

    __tablename__ = "student_contact_changes"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"),
                                            nullable=False)  # fmt: skip
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    address: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )
