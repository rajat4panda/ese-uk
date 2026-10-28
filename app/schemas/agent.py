"""Recruitment agent register: what may be written, and the rules it must meet (BRD FR-90 to FR-94)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator

from app.schemas.common import StrictModel, check_country, err, normalise_email

AgentStatus = Literal["active", "under_review", "suspended", "deactivated"]
EvidenceKind = Literal["agreement", "due_diligence"]


class _AgentFields(StrictModel):
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = None
    contact_phone: str | None = Field(default=None, max_length=40)
    due_diligence_date: date | None = None
    next_review_date: date | None = None
    refusal_rate_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=1)

    @field_validator("contact_email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalise_email(v)

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is not None and not all(ch.isdigit() or ch in "+ ()-" for ch in v):
            raise err("ERR_INVALID_FORMAT", "Use digits, spaces, brackets, + and - only.")
        return v

    @field_validator("due_diligence_date")
    @classmethod
    def _dd_not_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise err("ERR_INVALID_DATE", "The due-diligence date cannot be in the future.")
        return v

    @field_validator("next_review_date")
    @classmethod
    def _review_after_dd(cls, v: date | None, info: ValidationInfo) -> date | None:
        # Both dates in the same request. An update that sends one of them is checked against the
        # stored other one in the service.
        check_review_order(info.data.get("due_diligence_date"), v)
        return v


class AgentCreateIn(_AgentFields):
    name: str = Field(min_length=2, max_length=200)
    country: str

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return check_country(v)  # type: ignore[return-value]


class AgentUpdateIn(_AgentFields):
    """Every field optional; only the fields sent are changed. Status has its own endpoint, because
    suspending an agent is a decision with a reason, not an edit."""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    country: str | None = None

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return check_country(v)


class AgentStatusIn(StrictModel):
    status: AgentStatus
    reason: str | None = Field(default=None, max_length=1000, validate_default=True)

    @field_validator("reason")
    @classmethod
    def _reason_when_restricting(cls, v: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("status", "active") != "active" and (v is None or len(v) < 10):
            raise err("ERR_REASON_REQUIRED", "Give a reason of at least 10 characters. It is shown on the register.")
        return v


def check_review_order(dd: date | None, review: date | None) -> None:
    if dd is not None and review is not None and review <= dd:
        raise err("ERR_INVALID_DATE", "The next review must fall after the due-diligence date.")
