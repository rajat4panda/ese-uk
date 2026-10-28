"""Configuration and reference tables (LLD 3.12) needed from the foundation phase."""

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, pk


class Programme(Base):
    __tablename__ = "programmes"

    id: Mapped[int] = pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    qualification_level: Mapped[str] = mapped_column(Text, nullable=False)
    duration_months: Mapped[int] = mapped_column(Integer, nullable=False)
    atas_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")


class Intake(Base):
    __tablename__ = "intakes"

    id: Mapped[int] = pk()
    label: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    application_deadline: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")


class ChecklistDefinition(Base):
    """The source of valid checklist keys for both assessments (FR-126)."""

    __tablename__ = "checklist_definitions"

    id: Mapped[int] = pk()
    checklist_type: Mapped[str] = mapped_column(Text, nullable=False)  # academic | compliance
    item_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class OfferConditionLibrary(Base):
    """Standard conditions selectable when building an offer (FR-36)."""

    __tablename__ = "offer_condition_library"

    id: Mapped[int] = pk()
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)  # int | decimal | bool | text
    updated_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
