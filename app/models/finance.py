"""fee_schedules, application_fees and payments (LLD 3.9).

Balance is derived, never stored (LLD 3.9 note): outstanding = agreed_fee − scholarships − paid + refunds.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, ForeignKey, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import PaymentType

COUNTS_AS_PAID = (PaymentType.PAYMENT, PaymentType.DEPOSIT)


class FeeSchedule(Base):
    __tablename__ = "fee_schedules"

    id: Mapped[int] = pk()
    programme_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("programmes.id", ondelete="RESTRICT"), nullable=False
    )
    intake_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("intakes.id", ondelete="RESTRICT"), nullable=False)
    tuition_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    minimum_deposit: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="GBP")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = created_at_col()


class ApplicationFee(Base):
    __tablename__ = "application_fees"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    fee_schedule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fee_schedules.id", ondelete="RESTRICT"), nullable=False
    )
    agreed_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    variation_reason: Mapped[str | None] = mapped_column(Text)
    set_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    set_at: Mapped[datetime] = created_at_col()

    schedule = relationship("FeeSchedule", lazy="joined")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[PaymentType] = mapped_column(pg_enum("payment_type"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    provider_txn_id: Mapped[str | None] = mapped_column(Text, unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    authorised_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )
