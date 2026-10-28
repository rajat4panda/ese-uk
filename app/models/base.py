"""Declarative base and shared column types (LLD 1.1 conventions)."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Identity, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType

from app.models.enums import PG_ENUMS


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class CIText(UserDefinedType):
    """PostgreSQL citext — case-insensitive text used for email (LLD 3.1)."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return "CITEXT"


def pg_enum(name: str) -> SAEnum:
    """Bind a Python StrEnum to the PostgreSQL enum type created by migration."""
    enum_cls: type[StrEnum] = PG_ENUMS[name]
    return SAEnum(
        enum_cls,
        name=name,
        create_type=False,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class UTCDateTime(TypeDecorator):
    """timestamptz that always returns aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC)
        return value


def pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
