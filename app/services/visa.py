"""Visa outcomes and CAS withdrawal, and what each does to the student record (LLD 7.4).

FR-85, FR-86, FR-62. The rules the LLD sets out, in one place:

  granted   → the record stays awaiting arrival; the enrolment window opens. Not reportable.
  refused   → the record closes as non-enrolled with reason visa_refused. Not reportable — a
              refusal feeds the refusal metric instead, which is why the record must stay in the
              CAS population rather than being deleted.
  withdrawn
  not_used  → the record closes as non-enrolled with the matching reason.
  CAS withdrawn before a decision → record closes, and this one *is* reportable (T-21).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, GuardFailed, NotFound
from app.models.application import Document
from app.models.cas import CAS_ASSIGNED, Cas, VisaOutcomeRecord
from app.models.enums import EventType, UserRole, VisaOutcome
from app.models.user import User
from app.services import audit, events, students

LONDON = ZoneInfo("Europe/London")

VISA_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})
CAS_WITHDRAW_ROLES = frozenset({UserRole.SENIOR, UserRole.ADMIN})

CAS_WITHDRAWN = "withdrawn"

# The reason recorded on a record closed by a visa outcome, per outcome (LLD 7.4).
CLOSURE_REASON = {
    VisaOutcome.REFUSED: "visa_refused",
    VisaOutcome.WITHDRAWN: "visa_withdrawn",
    VisaOutcome.NOT_USED: "cas_not_used",
}


def record_outcome(
    db: Session,
    cas: Cas,
    actor: User,
    *,
    outcome: VisaOutcome,
    decision_date: date,
    refusal_reason_category: str | None = None,
    evidence_document_id: int | None = None,
    ip: str | None = None,
) -> VisaOutcomeRecord:
    if actor.role not in VISA_ROLES:
        raise Forbidden(details=[{"field": None, "code": "ERR_ROLE_FORBIDDEN", "action": "record a visa outcome"}])
    if cas.visa_outcome is not None:
        raise Conflict(
            "ERR_INVALID_STATE",
            f"A visa outcome is already recorded for CAS {cas.cas_number}. Correct it through a "
            "change of circumstances rather than by overwriting the decision.",
        )
    if decision_date > date.today():
        raise GuardFailed("ERR_DATE_RANGE", "The decision date cannot be in the future.",
                          details=[{"field": "decision_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip
    category = (refusal_reason_category or "").strip()
    if outcome == VisaOutcome.REFUSED and not category:
        # FR-86: without the category the refusal cannot be analysed, and the refusal rate is the
        # metric a sponsor is judged on.
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "Record the refusal reason category. It is what the refusal analysis is built from.",
            details=[{"field": "refusal_reason_category", "code": "ERR_REASON_REQUIRED"}],
        )
    if evidence_document_id is not None:
        document = db.get(Document, evidence_document_id)
        if document is None or document.application_id != cas.application_id:
            raise NotFound()

    record = VisaOutcomeRecord(
        cas_id=cas.id,
        outcome=outcome,
        decision_date=decision_date,
        refusal_reason_category=category or None,
        evidence_document_id=evidence_document_id,
        recorded_by=actor.id,
    )
    db.add(record)
    db.flush()

    audit.write(
        db, actor, "visa.outcome_recorded", application_id=cas.application_id,
        entity_type="visa_outcome", entity_id=record.id,
        detail=(f"Visa {outcome.value} on {decision_date.isoformat()} for CAS {cas.cas_number}"
                + (f" ({category})" if category else "")),
        ip=ip,
    )  # fmt: skip

    student = students.student_for_cas(db, cas)
    if student is not None and outcome in CLOSURE_REASON:
        students.close_record(
            db, student, actor,
            reason_code=CLOSURE_REASON[outcome],
            detail=(f"Record closed as non-enrolled: visa {outcome.value} on {decision_date.isoformat()}. "
                    "The record stays in the CAS population for the refusal and enrolment rates."),
            ip=ip,
        )  # fmt: skip
    return record


def withdraw_cas(
    db: Session, cas: Cas, actor: User, *, reason: str, ip: str | None = None,
    withdrawal_date: date | None = None, withdrawal_type: str = "withdrawn",
) -> tuple[Cas, object | None]:
    """FR-62, T-21. Withdrawing a CAS is reportable, and it closes the student record.

    BRD FR-58: it is recorded as withdrawn (the student is not coming, or ESE will not sponsor them)
    or cancelled (issued in error, typically to be reissued), with the date it took effect. That date
    may be earlier than today — the reporting deadline is counted from it, not from the day of entry."""
    if actor.role not in CAS_WITHDRAW_ROLES:
        raise Forbidden(details=[{"field": None, "code": "ERR_ROLE_FORBIDDEN", "action": "withdraw a CAS"}])
    if cas.status != CAS_ASSIGNED:
        raise Conflict("ERR_INVALID_STATE", f"This CAS is already {cas.status}.")
    if cas.visa_outcome is not None:
        # LLD 7.4 scopes CAS withdrawal to before a decision. Afterwards the student's permission
        # exists independently, and closing the record here would take them out of the enrolment
        # rate while they are still studying: that is a change of circumstances, not a withdrawal.
        raise Conflict(
            "ERR_INVALID_STATE",
            f"A visa decision ({cas.visa_outcome.outcome}) is already recorded against this CAS. "
            "Record what has changed as a change of circumstances instead.",
        )
    if withdrawal_type not in ("withdrawn", "cancelled"):
        raise GuardFailed("ERR_INVALID_FORMAT", "Choose whether the CAS was withdrawn or cancelled.",
                          details=[{"field": "withdrawal_type", "code": "ERR_INVALID_FORMAT"}])  # fmt: skip
    today = datetime.now(LONDON).date()  # both ends of the range in UK time, as the CAS dates are
    withdrawal_date = withdrawal_date or today
    assigned_on = cas.assigned_at.astimezone(LONDON).date()
    if withdrawal_date > today or withdrawal_date < assigned_on:
        raise GuardFailed("ERR_DATE_RANGE",
                          f"The date must be between the day the CAS was assigned ({assigned_on.isoformat()}) "
                          "and today.", details=[{"field": "withdrawal_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "Give a reason of at least 10 characters. It is reported to UKVI with the withdrawal.",
            details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}],
        )

    cas.status = CAS_WITHDRAWN
    cas.withdrawal_reason = reason
    cas.withdrawal_date = withdrawal_date
    cas.withdrawal_type = withdrawal_type
    db.flush()
    audit.write(
        db, actor, "cas.withdraw", application_id=cas.application_id, entity_type="cas", entity_id=cas.id,
        detail=f"CAS {cas.cas_number} {withdrawal_type} with effect from {withdrawal_date.isoformat()}: {reason}",
        ip=ip,
    )  # fmt: skip

    student = students.student_for_cas(db, cas)
    event = None
    if student is not None:
        students.close_record(
            db, student, actor,
            reason_code="cas_cancelled" if withdrawal_type == "cancelled" else "cas_withdrawn",
            detail=f"Record closed as non-enrolled: CAS {cas.cas_number} {withdrawal_type}. {reason}",
            ip=ip,
        )  # fmt: skip
        event = events.create_event(
            db, student, actor,
            event_type=EventType.CAS_WITHDRAWAL,
            event_date=withdrawal_date,
            source=events.SOURCE_MANUAL,
            detail=f"CAS {withdrawal_type}: {reason}",
            owner_id=actor.id,
            ip=ip,
        )  # fmt: skip
    return cas, event
