"""Reportable events: creation, the reporting queue and the act of reporting (FR-93 to FR-100).

This is the sponsor's reporting duty made mechanical. Three properties matter more than anything
else here:

  * An event is created in the same transaction as the thing that caused it. A deferral recorded
    without its event, or an event without its deferral, is the failure mode a compliance visit
    finds (T-20).
  * Automatic creation is idempotent. The partial unique index on (student, type, date) for
    automatic events means a job that runs twice cannot raise the same event twice (T-13), so the
    creation helper treats the conflict as success rather than as an error.
  * The deadline is stamped once, at creation, from the working-day calendar. It is never
    recomputed, because evidence of what the deadline was must not move.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from app.errors import Conflict, Forbidden, GuardFailed, NotFound
from app.models.application import Document
from app.models.compliance_events import (
    EVENT_NOT_REQUIRED,
    EVENT_PENDING,
    EVENT_REPORTED,
    SOURCE_AUTOMATIC,
    SOURCE_MANUAL,
    ReportableEvent,
)
from app.models.enums import DocumentCategory, EventType, UserRole
from app.models.student import StudentRecord
from app.models.user import User
from app.services import audit
from app.services.deadlines import reporting_deadline
from app.services.settings import get_setting

REPORTING_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})

# Which status change raises which event (LLD 7.4). The mapping is here, once, so the lifecycle
# and the scheduled jobs cannot disagree about it.
STATUS_CHANGE_EVENTS: dict[str, EventType] = {
    "deferral": EventType.DEFERRAL,
    "withdrawal": EventType.WITHDRAWAL,
    "interruption": EventType.INTERRUPTION,
    "course_change": EventType.COURSE_CHANGE,
    "curtailment": EventType.CURTAILMENT,
    "change_of_circumstances": EventType.CHANGE_OF_CIRCUMSTANCES,
}


def create_event(
    db: Session,
    student: StudentRecord,
    actor: User | None,
    *,
    event_type: EventType,
    event_date: date,
    source: str,
    detail: str = "",
    owner_id: int | None = None,
    ip: str | None = None,
) -> ReportableEvent | None:
    """Create a reportable event with its deadline. Returns None when an automatic event already
    exists for this student, type and date — the duplicate is the expected case for a job that
    runs twice, not an error."""
    if source == SOURCE_AUTOMATIC:
        existing = db.scalars(
            select(ReportableEvent).where(
                ReportableEvent.student_id == student.id,
                ReportableEvent.event_type == event_type,
                ReportableEvent.event_date == event_date,
                ReportableEvent.source == SOURCE_AUTOMATIC,
            )
        ).first()
        if existing is not None:
            return None

    deadline = reporting_deadline(db, event_date, int(get_setting(db, "REPORTING_WORKING_DAYS", 10)))
    event = ReportableEvent(
        student_id=student.id,
        event_type=event_type,
        event_date=event_date,
        deadline_date=deadline,
        source=source,
        detail=detail,
        owner_id=owner_id,
    )
    try:
        # A savepoint, not the transaction: a duplicate here must undo this insert only. Rolling
        # back the session would discard whatever the caller had already done in the same run —
        # in a scheduled job, that is other students' events and escalations. The savepoint opens
        # before the add, because begin_nested() flushes anything already pending.
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        # Detach the rejected row: left in the session it would be retried on the next flush.
        if event in db:
            db.expunge(event)
        # Lost the race with a concurrent run of the same job; the other one's event stands.
        if source == SOURCE_AUTOMATIC:
            return None
        raise

    audit.write(
        db,
        actor,
        "event.created_auto" if source == SOURCE_AUTOMATIC else "event.created_manual",
        application_id=student.application_id,
        student_id=student.id,
        entity_type="reportable_event",
        entity_id=event.id,
        detail=(
            f"{event_type.value.replace('_', ' ').capitalize()} event dated {event_date.isoformat()}; "
            f"must be reported to UKVI by {deadline.isoformat()}"
            + (f". {detail}" if detail else "")
        ),
        ip=ip,
        actor_label=None if actor else "scheduler",
    )
    return event


def queue(
    db: Session,
    *,
    status: str | None = None,
    overdue: bool | None = None,
    student_id: int | None = None,
    today: date | None = None,
) -> list[ReportableEvent]:
    """The reporting queue, soonest deadline first (FR-93)."""
    today = today or date.today()
    stmt: Select = select(ReportableEvent)
    if status:
        stmt = stmt.where(ReportableEvent.status == status)
    if student_id is not None:
        stmt = stmt.where(ReportableEvent.student_id == student_id)
    if overdue is True:
        stmt = stmt.where(ReportableEvent.status == EVENT_PENDING, ReportableEvent.deadline_date < today)
    elif overdue is False:
        stmt = stmt.where(ReportableEvent.status == EVENT_PENDING, ReportableEvent.deadline_date >= today)
    stmt = stmt.order_by(
        # Pending first, then by deadline: an overdue event must never be below a reported one.
        (ReportableEvent.status != EVENT_PENDING),
        ReportableEvent.deadline_date,
        ReportableEvent.id,
    )
    return list(db.scalars(stmt))


def get_event(db: Session, event_id: int, *, lock: bool = False) -> ReportableEvent:
    if lock:
        # The eager joins to the student and CAS are outer joins, which PostgreSQL will not lock;
        # only the event row needs locking, so lock just that (as students.get_student does).
        event = db.scalars(select(ReportableEvent).options(lazyload("*")).where(ReportableEvent.id == event_id)
                           .with_for_update(of=ReportableEvent)).first()  # fmt: skip
    else:
        event = db.get(ReportableEvent, event_id)
    if event is None:
        raise NotFound()
    return event


def report(
    db: Session,
    event: ReportableEvent,
    actor: User,
    *,
    sms_reference: str,
    reported_date: date,
    evidence_document_id: int | None = None,
    ip: str | None = None,
) -> ReportableEvent:
    """Record that the event was reported on the SMS (FR-96, FR-98)."""
    if event.status != EVENT_PENDING:
        raise Conflict("ERR_INVALID_STATE", "This event has already been closed.")
    reference = (sms_reference or "").strip()
    if not reference:
        raise GuardFailed(
            "ERR_REFERENCE_REQUIRED",
            "Record the SMS reference given by the Sponsorship Management System.",
            details=[{"field": "sms_reference", "code": "ERR_REFERENCE_REQUIRED"}],
        )
    if reported_date < event.event_date:
        raise GuardFailed(
            "ERR_DATE_RANGE",
            "The reported date cannot be before the event date.",
            details=[{"field": "reported_date", "code": "ERR_DATE_RANGE"}],
        )
    if reported_date > date.today():
        raise GuardFailed(
            "ERR_DATE_RANGE",
            "The reported date cannot be in the future.",
            details=[{"field": "reported_date", "code": "ERR_DATE_RANGE"}],
        )

    previous_evidence = event.evidence_document_id
    if evidence_document_id is not None and evidence_document_id != previous_evidence:
        _check_evidence(db, event, evidence_document_id)

    event.status = EVENT_REPORTED
    event.sms_reference = reference
    event.reported_by = actor.id
    event.reported_at = datetime.now(UTC)
    event.reported_date = reported_date
    if evidence_document_id is not None and evidence_document_id != previous_evidence:
        _supersede(db, event, actor, previous_evidence, evidence_document_id, ip)
        event.evidence_document_id = evidence_document_id
    db.flush()

    late = reported_date > event.deadline_date
    audit.write(
        db,
        actor,
        "event.reported",
        application_id=event.student.application_id,
        student_id=event.student_id,
        entity_type="reportable_event",
        entity_id=event.id,
        detail=(
            f"{event.event_type.value.replace('_', ' ').capitalize()} reported to UKVI on "
            f"{reported_date.isoformat()} against SMS reference {reference} "
            f"(deadline {event.deadline_date.isoformat()}"
            + (", REPORTED LATE)" if late else ")")
            + (f"; evidence document {event.evidence_document_id}" if event.evidence_document_id else "")
        ),
        ip=ip,
    )
    return event


def _check_evidence(db: Session, event: ReportableEvent, document_id: int) -> Document:
    """Evidence must be a clean, current reporting-evidence file on this student's own record, not
    already cited by another event. Without this any document id could be cited as proof (FR-76)."""
    doc = db.get(Document, document_id)
    student = event.student
    cited_elsewhere = db.scalars(select(ReportableEvent.id).where(
        ReportableEvent.evidence_document_id == document_id, ReportableEvent.id != event.id)).first()  # fmt: skip
    if (doc is None or doc.category != DocumentCategory.REPORT_EVIDENCE or doc.scan_status != "clean"
            or doc.superseded_by is not None or cited_elsewhere is not None
            or (doc.student_id != student.id and doc.application_id != student.application_id)):  # fmt: skip
        raise GuardFailed("ERR_INVALID_REFERENCE", "That file is not reporting evidence on this student's record.",
                          details=[{"field": "evidence_document_id", "code": "ERR_INVALID_REFERENCE"}])  # fmt: skip
    return doc


#: Who holds the reporting duty (LLD 6) — the same roles that may record a report.
EVIDENCE_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})


def attach_evidence(
    db: Session, event: ReportableEvent, actor: User, *, data: bytes, filename: str | None, max_bytes: int,
    ip: str | None = None,
) -> Document:
    """Keep the SMS confirmation with the event (FR-76). Allowed before or after the report is recorded,
    because the confirmation often arrives after the reference is typed in. A replacement supersedes
    the earlier file rather than deleting it."""
    from app.services import documents

    if actor.role not in EVIDENCE_ROLES:
        raise Forbidden()
    if event.status == EVENT_NOT_REQUIRED:
        raise Conflict("ERR_INVALID_STATE",
                       "This event was closed as not reportable, so there is no report to evidence.")
    student = event.student
    doc = documents.ingest(
        db, actor, data=data, filename=filename, max_bytes=max_bytes, category=DocumentCategory.REPORT_EVIDENCE,
        key_prefix=f"students/{student.id}/events/{event.id}", application_id=student.application_id,
        student_id=student.id, describe=f"Evidence for {event.event_type.value.replace('_', ' ')} event {event.id}",
        ip=ip,
    )  # fmt: skip
    _supersede(db, event, actor, event.evidence_document_id, doc.id, ip)
    event.evidence_document_id = doc.id
    db.flush()
    return doc


def _supersede(db: Session, event: ReportableEvent, actor: User, previous_id: int | None, new_id: int,
               ip: str | None) -> None:  # fmt: skip
    """Point the event at new evidence: the earlier file is marked superseded (never deleted), and the
    change is written on the event's own trail so its history shows what evidence stood when."""
    student = event.student
    new = db.get(Document, new_id)
    previous = db.get(Document, previous_id) if previous_id else None
    if previous is not None and previous.superseded_by is None:
        previous.superseded_by = new_id
        audit.write(db, actor, "document.supersede", application_id=student.application_id, student_id=student.id,
                    entity_type="document", entity_id=previous.id,
                    detail=f"Evidence {previous.filename} for event {event.id} superseded by {new.filename}",
                    ip=ip)  # fmt: skip
    audit.write(db, actor, "event.evidence", application_id=student.application_id, student_id=student.id,
                entity_type="reportable_event", entity_id=event.id,
                detail=(f"Evidence of report {'replaced' if previous else 'attached'}: {new.filename} "
                        f"(document {new_id})" + (f", replacing {previous.filename}" if previous else "")),
                ip=ip)  # fmt: skip


def close_not_required(
    db: Session, event: ReportableEvent, actor: User, *, reason: str, ip: str | None = None
) -> ReportableEvent:
    """Close an event that does not need reporting after all — with a reason, always (FR-94)."""
    if event.status != EVENT_PENDING:
        raise Conflict("ERR_INVALID_STATE", "This event has already been closed.")
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "Give a reason of at least 10 characters for closing this event without reporting it.",
            details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}],
        )
    event.status = EVENT_NOT_REQUIRED
    event.not_required_reason = reason
    event.reported_by = actor.id
    event.reported_at = datetime.now(UTC)
    db.flush()
    audit.write(
        db,
        actor,
        "event.reported",
        application_id=event.student.application_id,
        student_id=event.student_id,
        entity_type="reportable_event",
        entity_id=event.id,
        detail=f"Closed as not reportable: {reason}",
        ip=ip,
    )
    return event


def escalate(db: Session, event: ReportableEvent, ip: str | None = None) -> bool:
    """Mark an event as escalated to the Authorising Officer (FR-100). Idempotent by design: the
    sweep runs hourly and must not raise the same escalation twice."""
    if event.escalated_at is not None or event.status != EVENT_PENDING:
        return False
    event.escalated_at = datetime.now(UTC)
    db.flush()
    audit.write(
        db,
        None,
        "event.escalated",
        application_id=event.student.application_id,
        student_id=event.student_id,
        entity_type="reportable_event",
        entity_id=event.id,
        detail=(
            f"{event.event_type.value.replace('_', ' ').capitalize()} dated "
            f"{event.event_date.isoformat()} escalated: deadline {event.deadline_date.isoformat()}"
        ),
        ip=ip,
        actor_label="scheduler",
    )
    return True


__all__ = [
    "SOURCE_AUTOMATIC",
    "SOURCE_MANUAL",
    "STATUS_CHANGE_EVENTS",
    "close_not_required",
    "create_event",
    "escalate",
    "get_event",
    "queue",
    "report",
]
