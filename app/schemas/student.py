"""Contracts for the student record, attendance, reportable events and exports (LLD 5.4)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import AttendanceStatus, VisaOutcome
from app.schemas.application import Address
from app.schemas.common import E164, err, normalise_email


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnrolIn(_In):
    enrolment_date: date
    evidence_document_id: int | None = None
    permission_expiry: date | None = None


class AttendanceIn(_In):
    session_date: date
    module: str = Field(min_length=1, max_length=200)
    status: AttendanceStatus
    absence_reason: str | None = Field(default=None, max_length=1000)


class StatusChangeIn(_In):
    type: Literal["deferral", "withdrawal", "interruption", "course_change", "curtailment",
                  "change_of_circumstances"]  # fmt: skip
    effective_date: date
    reason: str = Field(min_length=10, max_length=2000)
    detail: str = Field(default="", max_length=2000)


class CompletionIn(_In):
    outcome: Literal["completed", "not_completed"]
    completion_date: date


class EscalationIn(_In):
    step: Literal["email", "meeting", "formal_warning", "final_warning", "reported"]
    notes: str = Field(default="", max_length=2000)


class VisaOutcomeIn(_In):
    outcome: VisaOutcome
    decision_date: date
    refusal_reason_category: str | None = Field(default=None, max_length=200)
    evidence_document_id: int | None = None


class CasWithdrawIn(_In):
    reason: str = Field(min_length=10, max_length=2000)
    # BRD FR-58
    withdrawal_type: Literal["withdrawn", "cancelled"] = "withdrawn"
    withdrawal_date: date | None = None


class EnrolmentEvidenceIn(_In):
    document_id: int


class StatusCheckIn(_In):
    """A right-to-study check (BRD FR-68)."""

    check_date: date
    method: Literal["evisa_share_code", "brp", "passport_vignette", "other"]
    reference: str | None = Field(default=None, max_length=200)
    permission_type: str | None = Field(default=None, max_length=200)
    permission_expiry: date | None = None
    outcome: Literal["confirmed", "discrepancy", "no_permission"]
    notes: str | None = Field(default=None, max_length=2000)
    evidence_document_id: int | None = None


class ContactIn(_In):
    """A change of address or contact details (BRD FR-67). The whole current set is sent, so the
    history row is a complete picture of the details from that date."""

    effective_from: date
    address: Address
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        v = (v or "").replace(" ", "") or None
        if v is not None and not E164.match(v):
            raise err("ERR_INVALID_FORMAT", "Enter the number in international format, for example +447700900123.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalise_email(v or None)

    @model_validator(mode="after")
    def _address_complete(self) -> ContactIn:
        missing = [f for f in ("line1", "city", "postcode", "country") if not getattr(self.address, f)]
        if missing:
            raise err("ERR_REQUIRED", f"Complete the address: {', '.join(missing)} is required.")
        return self


class StatusCheckOut(BaseModel):
    id: int
    check_date: date
    method: str
    method_label: str
    reference: str | None
    permission_type: str | None
    permission_expiry: date | None
    outcome: str
    outcome_label: str
    notes: str | None
    evidence: dict | None
    checked_by: str | None
    created_at: datetime


class ContactChangeOut(BaseModel):
    id: int
    effective_from: date
    address: dict
    phone: str | None
    email: str | None
    reason: str | None
    recorded_by: str | None
    recorded_at: datetime


class ReportIn(_In):
    sms_reference: str = Field(min_length=1, max_length=200)
    reported_date: date
    evidence_document_id: int | None = None


class NotRequiredIn(_In):
    reason: str = Field(min_length=10, max_length=2000)


# --- Responses ---------------------------------------------------------------------------------
class AttendanceOut(BaseModel):
    id: int
    session_date: date
    module: str
    status: str
    absence_reason: str | None
    recorded_by: str | None
    recorded_at: datetime


class AttendanceSummaryOut(BaseModel):
    scheduled: int
    attended: int
    present: int
    authorised_absence: int
    absent: int
    percentage: float | None
    threshold: int
    below_threshold: bool


class EventOut(BaseModel):
    id: int
    student_id: int
    application_id: int | None = None
    application_ref: str | None = None
    student_name: str | None = None
    event_type: str
    event_date: date
    deadline_date: date
    days_remaining: int
    overdue: bool
    source: str
    detail: str
    status: str
    reported_by: str | None = None
    reported_date: date | None = None
    sms_reference: str | None = None
    not_required_reason: str | None = None
    escalated_at: datetime | None = None
    created_at: datetime


class EscalationOut(BaseModel):
    id: int
    step: str
    notes: str
    raised_by: str | None
    raised_at: datetime


class VisaOutcomeOut(BaseModel):
    id: int
    outcome: str
    decision_date: date
    refusal_reason_category: str | None
    recorded_by: str | None
    recorded_at: datetime


class StudentOut(BaseModel):
    id: int
    application_id: int
    application_ref: str
    student_name: str | None
    programme: str | None
    intake: str | None
    cas_number: str | None
    cas_status: str | None
    course_start: date | None
    course_end: date | None
    enrolment_status: str
    enrolment_status_reason: str | None
    enrolment_date: date | None
    permission_expiry: date | None
    completion_outcome: str | None
    completion_date: date | None
    last_status_check: date | None
    latest_status_check_outcome: str | None = None
    current_address: dict = Field(default_factory=dict)
    contact_phone: str | None = None
    contact_email: str | None = None
    enrolment_evidence: dict | None = None
    cas_withdrawal_type: str | None = None
    cas_withdrawal_date: date | None = None
    cas_withdrawal_reason: str | None = None
    visa_outcome: VisaOutcomeOut | None = None
    attendance: AttendanceSummaryOut
    attendance_records: list[AttendanceOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    escalations: list[EscalationOut] = Field(default_factory=list)


class StudentSummary(BaseModel):
    id: int
    application_ref: str
    student_name: str | None
    programme: str | None
    enrolment_status: str
    course_start: date | None
    attendance_percentage: float | None
    open_events: int
    overdue_events: int


class StudentPage(BaseModel):
    items: list[StudentSummary]
    total: int
    page: int
    page_size: int


class MetricOut(BaseModel):
    key: str
    label: str
    numerator: int
    denominator: int
    percentage: float | None
    threshold: float
    direction: str
    rag: str
    small_sample: bool
    note: str


class MetricsOut(BaseModel):
    period_start: date
    period_end: date
    metrics: list[MetricOut]
    overall_rag: str


class ExportJobOut(BaseModel):
    id: str
    job_type: str
    status: str
    subject_ref: str | None
    result_filename: str | None
    expires_at: datetime | None
    error_detail: str | None
    created_at: datetime
    finished_at: datetime | None
