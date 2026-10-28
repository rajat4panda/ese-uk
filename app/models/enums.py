"""Enumerations (LLD Section 4).

Declared as PostgreSQL types by migration 0001 so invalid values cannot be persisted even by
direct database access. Values are identical in the database, the API and the client.
Adding a value requires a migration: a new state or event type is a compliance change.
"""

from enum import StrEnum


class UserRole(StrEnum):
    APPLICANT = "applicant"
    ADMISSIONS = "admissions"
    COMPLIANCE = "compliance"
    FINANCE = "finance"
    SENIOR = "senior"
    ADMIN = "admin"


STAFF_ROLES = frozenset({UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN})


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACADEMIC_REVIEW = "academic_review"
    # FR-35: academic sign-off is complete and an offer has not been built yet. A queue of its own,
    # for admissions rather than for an assessor.
    OFFER_PENDING = "offer_pending"
    INFO_REQUESTED = "info_requested"
    OFFER_ISSUED = "offer_issued"
    OFFER_ACCEPTED = "offer_accepted"
    OFFER_DECLINED = "offer_declined"
    COMPLIANCE_REVIEW = "compliance_review"
    PRECAS_READY = "precas_ready"
    CAS_ASSIGNED = "cas_assigned"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


EDITABLE_STATUSES = frozenset({ApplicationStatus.DRAFT, ApplicationStatus.INFO_REQUESTED})  # FR-10, FR-11
TERMINAL_STATUSES = frozenset(
    {ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.OFFER_DECLINED}
)


class RiskRating(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DocumentCategory(StrEnum):
    PASSPORT = "passport"
    QUALIFICATION = "qualification"
    ENGLISH = "english"
    STATEMENT = "statement"
    FINANCIAL = "financial"
    ENROLMENT_EVIDENCE = "enrolment_evidence"
    REPORT_EVIDENCE = "report_evidence"
    OTHER = "other"
    AGENT_AGREEMENT = "agent_agreement"  # FR-91, held against an agent, never an application
    AGENT_DUE_DILIGENCE = "agent_due_diligence"
    INTERVIEW_NOTES = "interview_notes"  # FR-37, uploaded by staff against the application


APPLICANT_DOCUMENT_CATEGORIES = frozenset(
    {
        DocumentCategory.PASSPORT,
        DocumentCategory.QUALIFICATION,
        DocumentCategory.ENGLISH,
        DocumentCategory.STATEMENT,
        DocumentCategory.FINANCIAL,
        DocumentCategory.OTHER,
    }
)


class EnrolmentStatus(StrEnum):
    AWAITING_ARRIVAL = "awaiting_arrival"
    ENROLLED = "enrolled"
    DEFERRED = "deferred"
    INTERRUPTED = "interrupted"
    WITHDRAWN = "withdrawn"
    COMPLETED = "completed"
    NON_ENROLLED = "non_enrolled"


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    AUTHORISED_ABSENCE = "authorised_absence"


class VisaOutcome(StrEnum):
    GRANTED = "granted"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"
    NOT_USED = "not_used"


class EventType(StrEnum):
    NON_ENROLMENT = "non_enrolment"
    WITHDRAWAL = "withdrawal"
    DEFERRAL = "deferral"
    COURSE_CHANGE = "course_change"
    INTERRUPTION = "interruption"
    CURTAILMENT = "curtailment"
    CHANGE_OF_CIRCUMSTANCES = "change_of_circumstances"
    CAS_WITHDRAWAL = "cas_withdrawal"
    SPONSOR_DETAILS_CHANGE = "sponsor_details_change"


class PaymentType(StrEnum):
    PAYMENT = "payment"
    DEPOSIT = "deposit"
    SCHOLARSHIP = "scholarship"
    REFUND = "refund"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class ScanStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    QUARANTINED = "quarantined"


# PostgreSQL type names and values, used by migration 0001 and by the ORM column types.
PG_ENUMS: dict[str, type[StrEnum]] = {
    "user_role": UserRole,
    "application_status": ApplicationStatus,
    "risk_rating": RiskRating,
    "document_category": DocumentCategory,
    "enrolment_status": EnrolmentStatus,
    "attendance_status": AttendanceStatus,
    "visa_outcome": VisaOutcome,
    "event_type": EventType,
    "payment_type": PaymentType,
}
