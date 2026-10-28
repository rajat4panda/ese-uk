"""agents (LLD 3.11). Created in the foundation phase because applications.agent_id references it;
the register, due-diligence and performance functions arrive in Sprint 5."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, FetchedValue, ForeignKey, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, created_at_col, pk

ATTRIBUTABLE_AGENT_STATUSES = ("active", "under_review")  # FR-106
#: Every status the database allows (CHECK on agents.status). Suspended and deactivated agents stay
#: on the register with their history; they simply cannot be attributed new applications (FR-94).
AGENT_STATUSES = ("active", "under_review", "suspended", "deactivated")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    agreement_document_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("documents.id"))
    due_diligence_document_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("documents.id"))
    due_diligence_date: Mapped[date | None] = mapped_column(Date)
    next_review_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    refusal_rate_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("now()"), server_onupdate=FetchedValue()
    )

    __mapper_args__ = {"eager_defaults": True}  # read updated_at back after the trigger sets it
