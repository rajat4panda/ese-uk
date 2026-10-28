"""Request and response contracts for the admissions → CAS workflow (LLD 5.3)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PaymentType, RiskRating


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReasonIn(_In):
    reason: str = Field(min_length=10, max_length=2000)


class AcademicIn(_In):
    checklist: dict[str, bool] = Field(default_factory=dict)
    outcome: Literal["recommend_offer", "request_info", "reject"] | None = None
    comments: str = Field(default="", max_length=4000)
    # FR-36
    english_evidence: Literal[
        "majority_english_country", "degree_taught_in_english", "slt_test",
        "provider_assessment", "not_required",
    ] | None = None  # fmt: skip
    english_standard: str | None = Field(default=None, max_length=200)
    # FR-37
    interview_date: date | None = None
    interviewer: str | None = Field(default=None, max_length=200)
    interview_outcome: Literal["satisfactory", "concerns", "unsatisfactory"] | None = None
    interview_notes: str | None = Field(default=None, max_length=4000)
    complete: bool = False


class ComplianceIn(_In):
    checklist: dict[str, bool] = Field(default_factory=dict)
    risk_rating: RiskRating | None = None
    atas_required: bool | None = None
    atas_reference: str | None = Field(default=None, max_length=100)
    comments: str = Field(default="", max_length=4000)
    complete: bool = False


class SecondLineIn(_In):
    outcome: Literal["supported", "not_supported"]


class OfferIn(_In):
    offer_type: Literal["conditional", "unconditional"]
    conditions: list[str] = Field(default_factory=list, max_length=20)
    expiry_date: date | None = None


class IssueIn(_In):
    issue_method: Literal["email", "portal", "post"]


class ConditionIn(_In):
    status: Literal["met", "outstanding"]
    evidence_document_id: int | None = None


class OfferResponseIn(_In):
    response: Literal["accepted", "declined"]


class CasIn(_In):
    cas_number: str = Field(max_length=30)
    course_title: str = Field(max_length=300)
    start_date: date
    end_date: date
    # FR-52: the figures as they were entered on the CAS itself. The fee ledger supplies the
    # defaults the screen offers, but what is stored must be what the sponsor declared to UKVI —
    # a CAS is checked against these numbers, not against our accounting.
    tuition_fee: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    amount_paid: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    level1_user: str = Field(max_length=200)
    notes: str = Field(default="", max_length=2000)
    deposit_override_reason: str | None = Field(default=None, min_length=10, max_length=1000)
    allocation_override_confirmed: bool = False


class PaymentIn(_In):
    type: PaymentType
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    payment_date: date
    method: Literal["bank_transfer", "card", "cash", "waiver"] | None = None
    reference: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class AllocationIn(_In):
    period_label: str = Field(max_length=100)
    period_start: date
    period_end: date
    allocated_count: int = Field(ge=0, le=100000)


# --- Responses --------------------------------------------------------------------------------
class ChecklistItemOut(BaseModel):
    item_key: str
    label: str
    mandatory: bool
    satisfied: bool | None


class AssessmentOut(BaseModel):
    id: int | None = None
    checklist: list[ChecklistItemOut] = Field(default_factory=list)
    comments: str = ""
    outcome: str | None = None
    english_evidence: str | None = None
    english_standard: str | None = None
    interview_date: date | None = None
    interviewer: str | None = None
    interview_outcome: str | None = None
    interview_notes: str | None = None
    interview_notes_documents: int = 0
    risk_rating: str | None = None
    atas_required: bool | None = None
    atas_reference: str | None = None
    completed_by: str | None = None
    completed_by_id: int | None = None
    completed_at: datetime | None = None
    second_line_by: str | None = None
    second_line_at: datetime | None = None
    second_line_outcome: str | None = None
    outstanding_mandatory: list[str] = Field(default_factory=list)


class ConditionOut(BaseModel):
    id: int
    description: str
    status: str
    evidence_document_id: int | None
    verified_at: datetime | None


class OfferOut(BaseModel):
    id: int
    offer_type: str
    conditions: list[ConditionOut]
    expiry_date: date | None
    issued_at: datetime | None
    issue_method: str | None
    response: str | None
    response_at: datetime | None
    override_reason: str | None
    has_letter: bool


class FeePositionOut(BaseModel):
    agreed_fee: Decimal | None
    minimum_deposit: Decimal | None
    paid: Decimal
    scholarship: Decimal
    refunded: Decimal
    outstanding: Decimal | None
    deposit_met: bool
    currency: str


class PaymentOut(BaseModel):
    id: int
    type: str
    amount: Decimal
    payment_date: date
    method: str | None
    reference: str | None
    reason: str | None
    recorded_at: datetime


class CasOut(BaseModel):
    id: int
    cas_number: str
    course_title: str
    start_date: date
    end_date: date
    tuition_fee: Decimal
    amount_paid: Decimal
    level1_user: str
    authorised_by: str | None
    status: str
    assigned_at: datetime
    allocation_period: str | None
    withdrawal_type: str | None = None
    withdrawal_date: date | None = None
    withdrawal_reason: str | None = None
    application_ref: str | None = None
    applicant_name: str | None = None


class AllocationOut(BaseModel):
    period_label: str | None
    period_start: date | None
    period_end: date | None
    allocated: int
    assigned: int
    remaining: int


class CasRegisterOut(BaseModel):
    items: list[CasOut]
    total: int
    allocation: AllocationOut


class StudentRecordOut(BaseModel):
    id: int
    enrolment_status: str
    enrolment_status_reason: str | None
    enrolment_date: date | None
