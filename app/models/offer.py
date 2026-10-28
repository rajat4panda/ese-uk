"""offers and offer_conditions (LLD 3.5)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pk

CONDITION_OUTSTANDING = "outstanding"
CONDITION_MET = "met"


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    offer_type: Mapped[str] = mapped_column(Text, nullable=False)  # conditional | unconditional
    letter_storage_key: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    issued_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    issue_method: Mapped[str | None] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    response_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expiry_date: Mapped[date | None] = mapped_column(Date)
    override_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    override_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    conditions = relationship(
        "OfferCondition", back_populates="offer", order_by="OfferCondition.id", lazy="selectin"
    )

    @property
    def outstanding_conditions(self) -> list[OfferCondition]:
        return [c for c in self.conditions if c.status != CONDITION_MET]


class OfferCondition(Base):
    __tablename__ = "offer_conditions"

    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("offers.id", ondelete="RESTRICT"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=CONDITION_OUTSTANDING)
    evidence_document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT")
    )
    verified_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    offer = relationship("Offer", back_populates="conditions")
