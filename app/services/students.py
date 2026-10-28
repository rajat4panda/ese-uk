"""The student record after a CAS is assigned (LLD 3.7, 7.4; FR-71 to FR-86).

Every status change here is paired with its reportable event in the same transaction, because a
status change the sponsor knows about but has not reported is the exact gap a compliance visit
looks for. The pairing lives in change_status(), not in the API layer, so no future endpoint can
record one without the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from app.errors import BadRequest, Conflict, Forbidden, GuardFailed, NotFound
from app.models.application import Document
from app.models.cas import Cas
from app.models.compliance_events import (
    ESCALATION_STEPS,
    AttendanceRecord,
    Escalation,
    ReportableEvent,
)
from app.models.enums import AttendanceStatus, DocumentCategory, EnrolmentStatus, EventType, UserRole
from app.models.student import ContactChange, StatusCheck, StudentRecord
from app.models.user import User
from app.services import audit, events
from app.services.settings import get_setting

# Who may touch a student record. Finance sees the fee side of an application and has no business
# in attendance or status changes (LLD 6). These are re-asserted here and not only on the router,
# because FR-122 requires the check to hold whatever the entry point — a command-line task or a
# worker reaches these functions without passing a dependency.
STUDENT_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.ADMISSIONS, UserRole.SENIOR, UserRole.ADMIN})
ATTENDANCE_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.ADMISSIONS, UserRole.SENIOR, UserRole.ADMIN})
REPORTING_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})


def require(actor: User, allowed: frozenset[UserRole], what: str) -> None:
    if actor.role not in allowed:
        raise Forbidden(details=[{"field": None, "code": "ERR_ROLE_FORBIDDEN", "action": what}])

# Status changes an operator can record, and what each does to the record (LLD 7.4). None means
# the enrolment status is untouched: a course change or a change of address is reportable, but it
# does not by itself say anything about whether the student is enrolled. Setting it to "enrolled"
# would promote a record that had been closed — and inflate the enrolment rate UKVI judges.
STATUS_CHANGES: dict[str, EnrolmentStatus | None] = {
    "deferral": EnrolmentStatus.DEFERRED,
    "withdrawal": EnrolmentStatus.WITHDRAWN,
    "interruption": EnrolmentStatus.INTERRUPTED,
    "course_change": None,
    "curtailment": EnrolmentStatus.WITHDRAWN,
    "change_of_circumstances": None,
}

# A record in one of these states is closed: attendance cannot be captured against it (LLD 7.4).
CLOSED_STATUSES = frozenset(
    {
        EnrolmentStatus.NON_ENROLLED,
        EnrolmentStatus.WITHDRAWN,
        EnrolmentStatus.COMPLETED,
        EnrolmentStatus.DEFERRED,
    }
)


@dataclass(frozen=True)
class AttendanceSummary:
    scheduled: int
    attended: int
    present: int
    authorised_absence: int
    absent: int
    percentage: float | None
    threshold: int
    below_threshold: bool

    @property
    def has_data(self) -> bool:
        return self.scheduled > 0


def get_student(db: Session, student_id: int, *, lock: bool = False) -> StudentRecord:
    stmt = select(StudentRecord).where(StudentRecord.id == student_id)
    if lock:
        # The eager join to cas would make this an outer join, which PostgreSQL will not lock;
        # the CAS row is not what needs locking here anyway.
        stmt = stmt.options(lazyload(StudentRecord.cas)).with_for_update(of=StudentRecord)
    student = db.scalars(stmt).first()
    if student is None:
        raise NotFound()
    return student


def for_application(db: Session, application_id: int) -> StudentRecord | None:
    return db.scalars(select(StudentRecord).where(StudentRecord.application_id == application_id)).first()


# --- Attendance (FR-74 to FR-78) ---------------------------------------------------------------
def attendance_summary(db: Session, student: StudentRecord, since: date | None = None) -> AttendanceSummary:
    """LLD 8.7. Authorised absence counts towards engagement and is still reported separately, so a
    pattern of authorised absence stays visible rather than being smoothed away (FR-76)."""
    stmt = select(AttendanceRecord.status, func.count()).where(AttendanceRecord.student_id == student.id)
    if since is not None:
        stmt = stmt.where(AttendanceRecord.session_date >= since)
    counts = {status: int(n) for status, n in db.execute(stmt.group_by(AttendanceRecord.status)).all()}
    present = counts.get(AttendanceStatus.PRESENT, 0)
    authorised = counts.get(AttendanceStatus.AUTHORISED_ABSENCE, 0)
    absent = counts.get(AttendanceStatus.ABSENT, 0)
    scheduled = present + authorised + absent
    attended = present + authorised
    threshold = int(get_setting(db, "ATTENDANCE_THRESHOLD_PCT", 80))
    percentage = round(100.0 * attended / scheduled, 1) if scheduled else None
    return AttendanceSummary(
        scheduled=scheduled,
        attended=attended,
        present=present,
        authorised_absence=authorised,
        absent=absent,
        percentage=percentage,
        threshold=threshold,
        below_threshold=percentage is not None and percentage < threshold,
    )


def record_attendance(
    db: Session,
    student: StudentRecord,
    actor: User,
    *,
    session_date: date,
    module: str,
    status: AttendanceStatus,
    absence_reason: str | None = None,
    ip: str | None = None,
) -> AttendanceRecord:
    require(actor, ATTENDANCE_ROLES, "record attendance")
    if student.enrolment_status in CLOSED_STATUSES:
        raise Conflict(
            "ERR_INVALID_STATE",
            f"Attendance cannot be recorded against a record that is "
            f"{student.enrolment_status.value.replace('_', ' ')}.",
        )
    if session_date > date.today():
        raise GuardFailed("ERR_DATE_RANGE", "A session cannot be recorded before it has happened.",
                          details=[{"field": "session_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip
    module = (module or "").strip()
    if not module:
        raise GuardFailed("ERR_REQUIRED", "Name the module or session.",
                          details=[{"field": "module", "code": "ERR_REQUIRED"}])  # fmt: skip
    if status == AttendanceStatus.AUTHORISED_ABSENCE and not (absence_reason or "").strip():
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "An authorised absence needs a reason: it counts towards engagement, so the basis must be on file.",
            details=[{"field": "absence_reason", "code": "ERR_REASON_REQUIRED"}],
        )

    record = AttendanceRecord(
        student_id=student.id,
        session_date=session_date,
        module=module,
        status=status,
        absence_reason=(absence_reason or "").strip() or None,
        approved_by=actor.id if status == AttendanceStatus.AUTHORISED_ABSENCE else None,
        recorded_by=actor.id,
    )
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        if record in db:
            db.expunge(record)
        raise Conflict(
            "ERR_ATTENDANCE_DUPLICATE",
            f"Attendance for {module} on {session_date.isoformat()} is already recorded for this student.",
        ) from exc

    audit.write(
        db, actor, "student.attendance", application_id=student.application_id, student_id=student.id,
        entity_type="attendance_record", entity_id=record.id,
        detail=f"{status.value.replace('_', ' ').capitalize()} recorded for {module} on {session_date.isoformat()}",
        ip=ip,
    )  # fmt: skip
    return record


def attendance_history(db: Session, student: StudentRecord, limit: int = 100) -> list[AttendanceRecord]:
    return list(
        db.scalars(
            select(AttendanceRecord)
            .where(AttendanceRecord.student_id == student.id)
            .order_by(AttendanceRecord.session_date.desc(), AttendanceRecord.id.desc())
            .limit(limit)
        )
    )


# --- Enrolment (FR-72) --------------------------------------------------------------------------
def enrol(
    db: Session,
    student: StudentRecord,
    actor: User,
    *,
    enrolment_date: date,
    evidence_document_id: int | None = None,
    permission_expiry: date | None = None,
    ip: str | None = None,
) -> StudentRecord:
    require(actor, STUDENT_ROLES, "record enrolment")
    if student.enrolment_status not in (EnrolmentStatus.AWAITING_ARRIVAL, EnrolmentStatus.INTERRUPTED):
        raise Conflict(
            "ERR_INVALID_STATE",
            f"This student is {student.enrolment_status.value.replace('_', ' ')} and cannot be enrolled.",
        )
    if enrolment_date > date.today():
        raise GuardFailed("ERR_DATE_RANGE", "The enrolment date cannot be in the future.",
                          details=[{"field": "enrolment_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip
    if evidence_document_id is not None:
        student.enrolment_evidence_id = _enrolment_evidence(db, student, evidence_document_id).id

    student.enrolment_status = EnrolmentStatus.ENROLLED
    student.enrolment_status_reason = None
    student.enrolment_date = enrolment_date
    if permission_expiry is not None:
        student.permission_expiry = permission_expiry
    db.flush()
    audit.write(
        db, actor, "student.enrol", application_id=student.application_id, student_id=student.id,
        entity_type="student_record", entity_id=student.id,
        detail=(f"Enrolled on {enrolment_date.isoformat()}"
                + (f"; evidence document {evidence_document_id}" if evidence_document_id else "")
                + (f"; permission expires {permission_expiry.isoformat()}" if permission_expiry else "")),
        ip=ip,
    )  # fmt: skip
    return student


# --- Status changes, each with its reportable event (FR-82, FR-83, FR-94) -----------------------
def change_status(
    db: Session,
    student: StudentRecord,
    actor: User,
    *,
    change_type: str,
    effective_date: date,
    reason: str,
    detail: str = "",
    ip: str | None = None,
) -> tuple[StudentRecord, ReportableEvent | None]:
    """Record a status change and raise its reportable event atomically (T-20)."""
    require(actor, REPORTING_ROLES, "record a change of circumstances")
    if change_type not in STATUS_CHANGES:
        raise GuardFailed(
            "ERR_UNKNOWN_FIELD",
            f"'{change_type}' is not a recognised status change.",
            details=[{"field": "type", "code": "ERR_UNKNOWN_FIELD"}],
        )
    if student.enrolment_status in (
        EnrolmentStatus.WITHDRAWN,
        EnrolmentStatus.COMPLETED,
        EnrolmentStatus.NON_ENROLLED,
    ):
        raise Conflict(
            "ERR_INVALID_STATE",
            f"This record is closed as {student.enrolment_status.value.replace('_', ' ')} and cannot be "
            "changed further. Correct it through a new CAS if sponsorship resumes.",
        )
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "Give a reason of at least 10 characters. It is reported to UKVI alongside the change.",
            details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}],
        )
    if effective_date > date.today():
        raise GuardFailed(
            "ERR_DATE_RANGE",
            "The effective date cannot be in the future: the reporting clock runs from the date it happened.",
            details=[{"field": "effective_date", "code": "ERR_DATE_RANGE"}],
        )

    previous = student.enrolment_status
    new_status = STATUS_CHANGES[change_type]
    if new_status is not None:
        student.enrolment_status = new_status
    student.enrolment_status_reason = f"{change_type}: {reason}"
    if change_type == "withdrawal":
        student.completion_outcome = "not_completed"
        student.completion_date = effective_date
    db.flush()

    audit.write(
        db, actor, "student.status_change", application_id=student.application_id, student_id=student.id,
        entity_type="student_record", entity_id=student.id,
        detail=(f"{change_type.replace('_', ' ').capitalize()} effective {effective_date.isoformat()}: "
                f"{previous.value} -> {student.enrolment_status.value}. {reason}"),
        ip=ip,
    )  # fmt: skip

    event = events.create_event(
        db, student, actor,
        event_type=events.STATUS_CHANGE_EVENTS[change_type],
        event_date=effective_date,
        source=events.SOURCE_MANUAL,
        detail=reason if not detail else f"{reason} {detail}",
        owner_id=actor.id,
        ip=ip,
    )  # fmt: skip
    return student, event


def complete(
    db: Session,
    student: StudentRecord,
    actor: User,
    *,
    outcome: str,
    completion_date: date,
    ip: str | None = None,
) -> StudentRecord:
    """Course completion (FR-84). Not reportable, but it feeds the completion metric."""
    require(actor, REPORTING_ROLES, "record course completion")
    if outcome not in ("completed", "not_completed"):
        raise GuardFailed("ERR_UNKNOWN_FIELD", "The completion outcome must be completed or not_completed.",
                          details=[{"field": "outcome", "code": "ERR_UNKNOWN_FIELD"}])  # fmt: skip
    if student.enrolment_status not in (EnrolmentStatus.ENROLLED, EnrolmentStatus.INTERRUPTED):
        raise Conflict("ERR_INVALID_STATE", "Only an enrolled or interrupted student can be completed.")
    student.completion_outcome = outcome
    student.completion_date = completion_date
    student.enrolment_status = (
        EnrolmentStatus.COMPLETED if outcome == "completed" else EnrolmentStatus.WITHDRAWN
    )
    db.flush()
    audit.write(
        db, actor, "student.status_change", application_id=student.application_id, student_id=student.id,
        entity_type="student_record", entity_id=student.id,
        detail=f"Course {outcome.replace('_', ' ')} on {completion_date.isoformat()}", ip=ip,
    )  # fmt: skip
    if outcome == "not_completed":
        # Completing is not reportable, but a student who did not complete has left the course,
        # and a withdrawal is (LLD 7.4). Raised here so the two cannot be recorded apart.
        events.create_event(
            db, student, actor, event_type=EventType.WITHDRAWAL, event_date=completion_date,
            source=events.SOURCE_MANUAL, detail="Course recorded as not completed.",
            owner_id=actor.id, ip=ip,
        )  # fmt: skip
    return student


# --- Escalations (FR-79) -------------------------------------------------------------------------
def raise_escalation(
    db: Session, student: StudentRecord, actor: User, *, step: str, notes: str, ip: str | None = None
) -> Escalation:
    require(actor, REPORTING_ROLES, "record an escalation")
    if step not in ESCALATION_STEPS:
        raise GuardFailed(
            "ERR_UNKNOWN_FIELD",
            f"'{step}' is not an escalation step. Use one of: {', '.join(ESCALATION_STEPS)}.",
            details=[{"field": "step", "code": "ERR_UNKNOWN_FIELD"}],
        )
    escalation = Escalation(student_id=student.id, step=step, notes=(notes or "").strip(), raised_by=actor.id)
    db.add(escalation)
    db.flush()
    audit.write(
        db, actor, "student.escalation", application_id=student.application_id, student_id=student.id,
        entity_type="escalation", entity_id=escalation.id,
        detail=f"Engagement escalation: {step.replace('_', ' ')}" + (f". {notes}" if notes else ""), ip=ip,
    )  # fmt: skip
    return escalation


def escalation_history(db: Session, student: StudentRecord) -> list[Escalation]:
    return list(
        db.scalars(
            select(Escalation).where(Escalation.student_id == student.id).order_by(Escalation.raised_at.desc())
        )
    )


# --- CAS withdrawal and visa outcomes feed this module through cas.py ---------------------------
def close_record(
    db: Session,
    student: StudentRecord,
    actor: User | None,
    *,
    reason_code: str,
    detail: str,
    ip: str | None = None,
) -> StudentRecord:
    """Close a record as non-enrolled (LLD 7.4). The record is never deleted: it stays in the CAS
    population, which is why the refusal and enrolment rates cannot be rebuilt from enrolled
    students alone."""
    student.enrolment_status = EnrolmentStatus.NON_ENROLLED
    student.enrolment_status_reason = reason_code
    db.flush()
    audit.write(
        db, actor, "student.status_change", application_id=student.application_id, student_id=student.id,
        entity_type="student_record", entity_id=student.id, detail=detail, ip=ip,
        actor_label=None if actor else "scheduler",
    )  # fmt: skip
    return student


def student_for_cas(db: Session, cas: Cas) -> StudentRecord | None:
    return db.scalars(select(StudentRecord).where(StudentRecord.cas_id == cas.id)).first()


# --- Case notes and the student's file (prototype screen 12) -------------------------------------
#: What staff may add on the student record. The application's own categories stay with the
#: application; these are the ones that arrive once the student is here.
STUDENT_DOC_CATEGORIES = frozenset({"passport", "enrolment_evidence", "financial", "other"})


def list_notes(db: Session, student: StudentRecord, actor: User) -> list[dict]:
    from app.models.student import StudentNote

    require(actor, STUDENT_ROLES, "read case notes")
    rows = db.scalars(select(StudentNote).where(StudentNote.student_id == student.id)
                      .order_by(StudentNote.created_at.desc(), StudentNote.id.desc())).all()  # fmt: skip
    authors = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_({r.author_id for r in rows})))}
    return [{"id": r.id, "body": r.body, "author": authors.get(r.author_id), "created_at": r.created_at} for r in rows]


def add_note(db: Session, student: StudentRecord, actor: User, body: str, ip: str | None) -> dict:
    """A case note. Notes are never edited: a correction is a further note (append-only in the DB)."""
    from app.models.student import StudentNote

    require(actor, STUDENT_ROLES, "add a case note")
    note = StudentNote(student_id=student.id, body=body.strip(), author_id=actor.id)
    db.add(note)
    db.flush()
    audit.write(db, actor, "student.note", application_id=student.application_id, student_id=student.id,
                entity_type="student_note", entity_id=note.id, detail=note.body[:500], ip=ip)  # fmt: skip
    return {"id": note.id, "body": note.body, "author": actor.full_name, "created_at": note.created_at}


def list_documents(db: Session, student: StudentRecord, actor: User) -> list[Document]:
    require(actor, STUDENT_ROLES, "read the student's documents")
    return list(db.scalars(select(Document).where(
        (Document.student_id == student.id) | (Document.application_id == student.application_id)
    ).order_by(Document.uploaded_at.desc())))  # fmt: skip


def add_document(db: Session, student: StudentRecord, actor: User, *, category: str, filename: str | None,
                 data: bytes, max_bytes: int, ip: str | None) -> Document:  # fmt: skip
    from app.services import documents

    require(actor, STUDENT_ROLES, "add a document to the student record")
    if category not in STUDENT_DOC_CATEGORIES:
        raise BadRequest("ERR_INVALID_FORMAT", "Choose a document type from the list.",
                         details=[{"field": "category", "code": "ERR_INVALID_FORMAT"}])  # fmt: skip
    return documents.ingest(db, actor, data=data, filename=filename, max_bytes=max_bytes,
                            category=DocumentCategory(category), key_prefix=f"students/{student.id}",
                            application_id=student.application_id, student_id=student.id,
                            describe=f"Student record {student.id}", ip=ip)  # fmt: skip


# --- Enrolment evidence (BRD FR-61) -------------------------------------------------------------
def _enrolment_evidence(db: Session, student: StudentRecord, document_id: int) -> Document:
    """Evidence of enrolment must be a clean, current enrolment-evidence file on this student's own file."""
    doc = db.get(Document, document_id)
    if (doc is None or doc.category != DocumentCategory.ENROLMENT_EVIDENCE or doc.scan_status != "clean"
            or doc.superseded_by is not None
            or (doc.student_id != student.id and doc.application_id != student.application_id)):  # fmt: skip
        raise GuardFailed("ERR_INVALID_REFERENCE", "Choose an enrolment-evidence file on this student's record.",
                          details=[{"field": "evidence_document_id", "code": "ERR_INVALID_REFERENCE"}])  # fmt: skip
    return doc


def set_enrolment_evidence(db: Session, student: StudentRecord, actor: User, document_id: int,
                           ip: str | None) -> StudentRecord:  # fmt: skip
    """Attach or replace the evidence of enrolment on a student already enrolled (FR-61)."""
    require(actor, STUDENT_ROLES, "record enrolment evidence")
    if student.enrolment_date is None:
        raise Conflict("ERR_INVALID_STATE", "Record the enrolment first; the evidence is attached with it.")
    doc = _enrolment_evidence(db, student, document_id)
    previous = db.get(Document, student.enrolment_evidence_id) if student.enrolment_evidence_id else None
    if previous is not None and previous.id == doc.id:
        raise Conflict("ERR_UNCHANGED", "That file is already the enrolment evidence.")
    student.enrolment_evidence_id = doc.id
    db.flush()
    audit.write(db, actor, "student.enrolment_evidence", application_id=student.application_id,
                student_id=student.id, entity_type="student_record", entity_id=student.id,
                detail=f"Enrolment evidence: {doc.filename} (document {doc.id})"
                       + (f", replacing {previous.filename} (document {previous.id})" if previous else ""),
                ip=ip)  # fmt: skip
    return student


# --- Right-to-study and immigration status checks (BRD FR-68) ----------------------------------
STATUS_CHECK_METHODS = {
    "evisa_share_code": "Online check with a share code (eVisa)",
    "brp": "Biometric residence permit",
    "passport_vignette": "Passport and entry clearance vignette",
    "other": "Other document",
}
STATUS_CHECK_OUTCOMES = {
    "confirmed": "Right to study confirmed",
    "discrepancy": "Discrepancy found",
    "no_permission": "No valid permission",
}


def _share_code(raw: str | None) -> str | None:
    code = "".join(ch for ch in (raw or "") if ch.isalnum()).upper()
    return code if len(code) == 9 else None


def record_status_check(
    db: Session, student: StudentRecord, actor: User, *, check_date: date, method: str, outcome: str,
    reference: str | None = None, permission_type: str | None = None, permission_expiry: date | None = None,
    notes: str | None = None, evidence_document_id: int | None = None, ip: str | None = None,
) -> StatusCheck:
    """Record a check and, when it is the latest, what it found: a confirmed check sets the permission
    expiry the expiry alerts run from. The check is append-only; a mistake is corrected by recording
    the check again."""
    require(actor, STUDENT_ROLES, "record a right-to-study check")

    def bad(field: str, code: str, message: str) -> GuardFailed:
        return GuardFailed(code, message, details=[{"field": field, "code": code, "message": message}])

    if method not in STATUS_CHECK_METHODS:
        raise bad("method", "ERR_INVALID_FORMAT", "Choose how the check was made.")
    if outcome not in STATUS_CHECK_OUTCOMES:
        raise bad("outcome", "ERR_INVALID_FORMAT", "Choose the outcome of the check.")
    if check_date > date.today():
        raise bad("check_date", "ERR_DATE_RANGE", "The check date cannot be in the future.")
    reference = (reference or "").strip() or None
    if method == "evisa_share_code":
        reference = _share_code(reference)
        if reference is None:
            raise bad("reference", "ERR_INVALID_FORMAT", "Enter the 9-character share code, for example W4K 9P2 7XA.")
    if outcome == "confirmed":
        if permission_expiry is None:
            raise bad("permission_expiry", "ERR_REQUIRED", "Record when the permission expires.")
        if permission_expiry < check_date:
            raise bad("permission_expiry", "ERR_DATE_RANGE",
                      "An expired permission cannot be confirmed. Record the outcome as no valid permission.")
    if outcome != "confirmed" and not (notes or "").strip():
        raise bad("notes", "ERR_REQUIRED", "Say what was found. It is the record of why this student was flagged.")
    evidence = None
    if evidence_document_id is not None:
        evidence = db.get(Document, evidence_document_id)
        on_file = evidence is not None and (evidence.student_id == student.id
                                            or evidence.application_id == student.application_id)  # fmt: skip
        if not on_file or evidence.scan_status != "clean" or evidence.superseded_by is not None:
            raise bad("evidence_document_id", "ERR_INVALID_REFERENCE", "Choose a file on this student's record.")

    latest = db.scalar(select(func.max(StatusCheck.check_date)).where(StatusCheck.student_id == student.id))
    check = StatusCheck(student_id=student.id, check_date=check_date, method=method, reference=reference,
                        permission_type=(permission_type or "").strip() or None, permission_expiry=permission_expiry,
                        outcome=outcome, notes=(notes or "").strip() or None,
                        evidence_document_id=evidence.id if evidence else None, checked_by=actor.id)  # fmt: skip
    db.add(check)
    # Only the latest check speaks for the student now: recording an older one afterwards keeps the history
    # complete without rolling the expiry back.
    applies = latest is None or check_date >= latest
    old_expiry = student.permission_expiry
    if applies:
        student.last_status_check = check_date
        if outcome == "confirmed":
            student.permission_expiry = permission_expiry
    db.flush()
    audit.write(db, actor, "student.status_check", application_id=student.application_id, student_id=student.id,
                entity_type="status_check", entity_id=check.id,
                detail=(f"Right-to-study check {check_date.isoformat()} by {STATUS_CHECK_METHODS[method].lower()}"
                        + (f" ({reference})" if reference else "") + f": {STATUS_CHECK_OUTCOMES[outcome].lower()}"
                        + (f"; permission {permission_type or ''} expires {permission_expiry.isoformat()}"
                           if permission_expiry else "")
                        + (f"; permission expiry on record {old_expiry} -> {student.permission_expiry}"
                           if applies and old_expiry != student.permission_expiry else "")
                        + ("" if applies else "; recorded after a later check, so the record is unchanged")),
                ip=ip)  # fmt: skip
    return check


def status_checks(db: Session, student: StudentRecord) -> list[StatusCheck]:
    return list(db.scalars(select(StatusCheck).where(StatusCheck.student_id == student.id)
                           .order_by(StatusCheck.check_date.desc(), StatusCheck.id.desc())))  # fmt: skip


# --- Contact details and address history (BRD FR-67) -------------------------------------------
def record_contact_change(
    db: Session, student: StudentRecord, actor: User, *, effective_from: date, address: dict,
    phone: str | None, email: str | None, reason: str | None, ip: str | None = None,
) -> ContactChange:
    """A change of address or contact details. The current details on the record follow the change
    with the latest effective date; an older change recorded late joins the history only."""
    require(actor, STUDENT_ROLES, "record a change of contact details")
    if effective_from > date.today():
        raise GuardFailed("ERR_DATE_RANGE", "Record a change once it has happened, not before.",
                          details=[{"field": "effective_from", "code": "ERR_DATE_RANGE"}])  # fmt: skip
    latest = db.scalar(select(func.max(ContactChange.effective_from)).where(ContactChange.student_id == student.id))
    change = ContactChange(student_id=student.id, effective_from=effective_from, address=address, phone=phone,
                           email=email, reason=(reason or "").strip() or None, recorded_by=actor.id)  # fmt: skip
    db.add(change)
    applies = latest is None or effective_from >= latest
    before = (student.current_address or {}, student.contact_phone, student.contact_email)
    if applies:
        student.current_address = address
        student.contact_phone = phone
        student.contact_email = email
    db.flush()

    def line(a: dict) -> str:
        return ", ".join(str(a[k]) for k in ("line1", "line2", "city", "postcode", "country") if a.get(k)) or "—"

    parts = []
    if not applies:
        parts.append(f"address {line(address)}; phone {phone or '—'}; email {email or '—'}")
    elif line(before[0]) != line(address):
        parts.append(f"address {line(before[0])} -> {line(address)}")
    if applies and before[1] != phone:
        parts.append(f"phone {before[1] or '—'} -> {phone or '—'}")
    if applies and before[2] != email:
        parts.append(f"email {before[2] or '—'} -> {email or '—'}")
    audit.write(db, actor, "student.contact_change", application_id=student.application_id, student_id=student.id,
                entity_type="contact_change", entity_id=change.id,
                detail=f"Contact details effective {effective_from.isoformat()}: "
                       + ("; ".join(parts) or "no change to the current details")
                       + ("" if applies else " (recorded after a later change; current details unchanged)")
                       + (f". Reason: {change.reason}" if change.reason else ""), ip=ip)  # fmt: skip
    return change


def contact_history(db: Session, student: StudentRecord) -> list[ContactChange]:
    return list(db.scalars(select(ContactChange).where(ContactChange.student_id == student.id)
                           .order_by(ContactChange.effective_from.desc(), ContactChange.id.desc())))  # fmt: skip
