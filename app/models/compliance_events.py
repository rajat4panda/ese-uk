"""Post-CAS compliance records: attendance, reportable events and escalations (LLD 3.7, 3.8).

The reportable event is the object a Home Office compliance visit asks about: what happened, when,
when it had to be reported by, and what was actually reported. Its deadline is computed once, on
creation, from the working-day calendar (LLD 8.2) and is never recomputed — a deadline that moved
because a bank holiday row was added later would be worthless as evidence.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import AttendanceStatus, EventType

EVENT_PENDING = "pending"
EVENT_REPORTED = "reported"
EVENT_NOT_REQUIRED = "not_required"

SOURCE_AUTOMATIC = "automatic"
SOURCE_MANUAL = "manual"

ESCALATION_STEPS = ("email", "meeting", "formal_warning", "final_warning", "reported")

# Attendance that counts towards the engagement threshold (LLD 8.7). Authorised absence counts,
# and is still reported separately so a pattern of it stays visible (FR-76).
ATTENDED = (AttendanceStatus.PRESENT, AttendanceStatus.AUTHORISED_ABSENCE)


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    module: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[AttendanceStatus] = mapped_column(pg_enum("attendance_status"), nullable=False)
    absence_reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    recorded_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )


class ReportableEvent(Base):
    __tablename__ = "reportable_events"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[EventType] = mapped_column(pg_enum("event_type"), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    deadline_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    owner_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=EVENT_PENDING)
    reported_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reported_date: Mapped[date | None] = mapped_column(Date)
    sms_reference: Mapped[str | None] = mapped_column(Text)
    not_required_reason: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT")
    )
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = created_at_col()

    student = relationship("StudentRecord", lazy="joined")

    def days_remaining(self, today: date) -> int:
        """Calendar days to the deadline. Negative once it is breached."""
        return (self.deadline_date - today).days

    def is_overdue(self, today: date) -> bool:
        return self.status == EVENT_PENDING and self.deadline_date < today


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = pk()
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT"), nullable=False
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    raised_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    raised_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )


class BankHoliday(Base):
    __tablename__ = "bank_holidays"

    holiday_date: Mapped[date] = mapped_column(Date, primary_key=True)
    jurisdiction: Mapped[str] = mapped_column(Text, primary_key=True, server_default="england-and-wales")
    name: Mapped[str] = mapped_column(Text, nullable=False)
