"""cas, visa_outcomes and cas_allocations (LLD 3.6, 3.12).

A CAS row is written only by the assign_cas transition, inside the transaction that also writes
the student record and the audit entry (LLD 7.3 figure 2).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import VisaOutcome

CAS_ASSIGNED = "assigned"
CAS_WITHDRAWN = "withdrawn"


class CasAllocation(Base):
    __tablename__ = "cas_allocations"

    id: Mapped[int] = pk()
    period_label: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    allocated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")


class Cas(Base):
    __tablename__ = "cas"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    cas_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    course_title: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    tuition_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0")
    level1_user: Mapped[str] = mapped_column(Text, nullable=False)
    authorised_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    allocation_period_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cas_allocations.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=CAS_ASSIGNED)
    withdrawal_reason: Mapped[str | None] = mapped_column(Text)
    withdrawal_date: Mapped[date | None] = mapped_column(Date)  # FR-58: when it took effect
    withdrawal_type: Mapped[str | None] = mapped_column(Text)  # withdrawn | cancelled
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    allocation = relationship("CasAllocation", lazy="joined")
    authoriser = relationship("User", foreign_keys=[authorised_by], lazy="joined")
    visa_outcome = relationship("VisaOutcomeRecord", back_populates="cas", uselist=False, lazy="selectin")


class VisaOutcomeRecord(Base):
    __tablename__ = "visa_outcomes"

    id: Mapped[int] = pk()
    cas_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cas.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    outcome: Mapped[VisaOutcome] = mapped_column(pg_enum("visa_outcome"), nullable=False)
    decision_date: Mapped[date] = mapped_column(Date, nullable=False)
    refusal_reason_category: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT")
    )
    recorded_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = created_at_col()

    cas = relationship("Cas", back_populates="visa_outcome")
