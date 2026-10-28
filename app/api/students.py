"""Student records, attendance, status changes, reportable events, metrics and evidence packs
(LLD 5.4). Everything post-CAS.

The route that matters most here is POST /students/{id}/status-change: it records the change and
raises its reportable event in one transaction, so the two can never come apart (FR-94, T-20).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, require_roles
from app.database import get_db
from app.errors import Conflict, GuardFailed, NotFound
from app.models.application import Application
from app.models.cas import Cas
from app.models.compliance_events import EVENT_PENDING, ReportableEvent
from app.models.enums import UserRole
from app.models.operations import EXPORT_READY, ExportJob
from app.models.student import StudentRecord
from app.models.user import User
from app.schemas.common import StrictModel
from app.schemas.student import (
    AttendanceIn,
    CasWithdrawIn,
    CompletionIn,
    ContactChangeOut,
    ContactIn,
    EnrolIn,
    EnrolmentEvidenceIn,
    EscalationIn,
    EventOut,
    ExportJobOut,
    MetricsOut,
    NotRequiredIn,
    ReportIn,
    StatusChangeIn,
    StatusCheckIn,
    StatusCheckOut,
    StudentOut,
    StudentPage,
    StudentSummary,
    VisaOutcomeIn,
)
from app.services import applications as app_svc
from app.services import audit, events, evidence, metrics, students, visa
from app.services.settings import get_setting
from app.services.storage import get_storage
from app.views import event_out, student_out

router = APIRouter(tags=["students"])

# Who sees and acts on student records (LLD 6). Finance is deliberately absent: it has the fee
# side of an application and no business in attendance or immigration status.
STUDENT_VIEW = (UserRole.COMPLIANCE, UserRole.ADMISSIONS, UserRole.SENIOR, UserRole.ADMIN)
STUDENT_EDIT = (UserRole.COMPLIANCE, UserRole.ADMISSIONS, UserRole.SENIOR, UserRole.ADMIN)
COMPLIANCE_ONLY = (UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN)
SENIOR = (UserRole.SENIOR, UserRole.ADMIN)
STAFF = (UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN)


def _load(db: Session, student_id: int, *, lock: bool = False) -> StudentRecord:
    return students.get_student(db, student_id, lock=lock)


# --- Compliance overview (BRD FR-70, FR-71) --------------------------------------------------------
@router.get("/compliance/overview")
def compliance_overview(current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
                        db: Session = Depends(get_db)) -> dict:  # fmt: skip
    """Every sponsored student classified, with the reasons. Also read by the Authorising Officer's
    dashboard, which is why admissions — who see that dashboard — can read it."""
    from app.services import monitoring

    return monitoring.overview(db)


# --- The student list and one record ------------------------------------------------------------
@router.get("/students", response_model=StudentPage)
def list_students(
    q: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
    db: Session = Depends(get_db),
) -> StudentPage:
    stmt = select(StudentRecord).join(Application, Application.id == StudentRecord.application_id)
    if status:
        stmt = stmt.where(StudentRecord.enrolment_status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Application.ref).like(like)
            | func.lower(Application.data["full_name"].astext).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(StudentRecord.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    today = date.today()
    items = []
    for student in rows:
        app = db.get(Application, student.application_id)
        cas = db.get(Cas, student.cas_id)
        summary = students.attendance_summary(db, student)
        open_events = db.scalars(
            select(ReportableEvent).where(
                ReportableEvent.student_id == student.id, ReportableEvent.status == EVENT_PENDING
            )
        ).all()
        items.append(
            StudentSummary(
                id=student.id,
                application_ref=app.ref if app else "—",
                student_name=(app.data.get("full_name") if app else None),
                programme=app.programme.title if app and app.programme else None,
                enrolment_status=str(student.enrolment_status),
                course_start=cas.start_date if cas else None,
                attendance_percentage=summary.percentage,
                open_events=len(open_events),
                overdue_events=sum(1 for e in open_events if e.deadline_date < today),
            )
        )
    return StudentPage(items=items, total=int(total), page=page, page_size=page_size)


@router.get("/students/{student_id}", response_model=StudentOut)
def get_student(
    student_id: int,
    current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
    db: Session = Depends(get_db),
) -> StudentOut:
    return student_out(db, _load(db, student_id))


@router.get("/applications/{application_id}/student", response_model=StudentOut)
def student_for_application(
    application_id: int,
    current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
    db: Session = Depends(get_db),
) -> StudentOut:
    app = app_svc.get_for_actor(db, application_id, current.user)
    student = students.for_application(db, app.id)
    if student is None:
        raise NotFound()
    return student_out(db, student)


# --- Enrolment, attendance, escalation (FR-72 to FR-79) -------------------------------------------
@router.post("/students/{student_id}/enrol", response_model=StudentOut)
def enrol(
    student_id: int,
    body: EnrolIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id, lock=True)
    students.enrol(
        db, student, current.user, enrolment_date=body.enrolment_date,
        evidence_document_id=body.evidence_document_id, permission_expiry=body.permission_expiry,
        ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return student_out(db, student)


@router.post("/students/{student_id}/attendance", response_model=StudentOut, status_code=201)
def record_attendance(
    student_id: int,
    body: AttendanceIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id)
    students.record_attendance(
        db, student, current.user, session_date=body.session_date, module=body.module,
        status=body.status, absence_reason=body.absence_reason, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return student_out(db, student)


@router.post("/students/{student_id}/escalation", response_model=StudentOut, status_code=201)
def escalate(
    student_id: int,
    body: EscalationIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id)
    students.raise_escalation(db, student, current.user, step=body.step, notes=body.notes, ip=client_ip(request))
    db.commit()
    return student_out(db, student)


# --- Status changes, each with its reportable event (FR-82, FR-83, T-20) --------------------------
@router.post("/students/{student_id}/status-change", response_model=StudentOut)
def status_change(
    student_id: int,
    body: StatusChangeIn,
    request: Request,
    # Compliance work, not admissions work: this raises a reportable event and starts the UKVI
    # clock (LLD 6).
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id, lock=True)
    students.change_status(
        db, student, current.user, change_type=body.type, effective_date=body.effective_date,
        reason=body.reason, detail=body.detail, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return student_out(db, student)


@router.post("/students/{student_id}/completion", response_model=StudentOut)
def complete_course(
    student_id: int,
    body: CompletionIn,
    request: Request,
    # The completion outcome is the numerator of a BCA metric (LLD 8.3).
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id, lock=True)
    students.complete(
        db, student, current.user, outcome=body.outcome, completion_date=body.completion_date,
        ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return student_out(db, student)


# --- Visa outcome and CAS withdrawal (FR-85, FR-86, FR-62) ----------------------------------------
@router.post("/applications/{application_id}/visa-outcome", response_model=StudentOut)
def record_visa_outcome(
    application_id: int,
    body: VisaOutcomeIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> StudentOut:
    app = app_svc.get_for_actor(db, application_id, current.user, lock=True)
    if app.cas is None:
        raise Conflict("ERR_INVALID_STATE", "No CAS has been assigned, so there is no visa decision to record.")
    visa.record_outcome(
        db, app.cas, current.user, outcome=body.outcome, decision_date=body.decision_date,
        refusal_reason_category=body.refusal_reason_category,
        evidence_document_id=body.evidence_document_id, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    student = students.for_application(db, app.id)
    if student is None:
        raise NotFound()
    return student_out(db, student)


@router.post("/applications/{application_id}/cas/withdraw", response_model=StudentOut)
def withdraw_cas(
    application_id: int,
    body: CasWithdrawIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> StudentOut:
    app = app_svc.get_for_actor(db, application_id, current.user, lock=True)
    if app.cas is None:
        raise Conflict("ERR_INVALID_STATE", "No CAS has been assigned to this application.")
    visa.withdraw_cas(db, app.cas, current.user, reason=body.reason, withdrawal_date=body.withdrawal_date,
                      withdrawal_type=body.withdrawal_type, ip=client_ip(request))  # fmt: skip
    db.commit()
    student = students.for_application(db, app.id)
    if student is None:
        raise NotFound()
    return student_out(db, student)


# --- Enrolment evidence (BRD FR-61) ---------------------------------------------------------------
@router.post("/students/{student_id}/enrolment-evidence", response_model=StudentOut)
def set_enrolment_evidence(
    student_id: int,
    body: EnrolmentEvidenceIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id, lock=True)
    students.set_enrolment_evidence(db, student, current.user, body.document_id, client_ip(request))
    db.commit()
    return student_out(db, student)


# --- Right-to-study checks (BRD FR-68) -----------------------------------------------------------
def _check_out(db: Session, c) -> StatusCheckOut:
    from app.models.application import Document

    doc = db.get(Document, c.evidence_document_id) if c.evidence_document_id else None
    who = db.get(User, c.checked_by)
    return StatusCheckOut(
        id=c.id, check_date=c.check_date, method=c.method, method_label=students.STATUS_CHECK_METHODS[c.method],
        reference=c.reference, permission_type=c.permission_type, permission_expiry=c.permission_expiry,
        outcome=c.outcome, outcome_label=students.STATUS_CHECK_OUTCOMES[c.outcome], notes=c.notes,
        evidence={"id": doc.id, "filename": doc.filename} if doc else None,
        checked_by=who.full_name if who else None, created_at=c.created_at,
    )  # fmt: skip


@router.get("/students/{student_id}/status-checks", response_model=list[StatusCheckOut])
def list_status_checks(student_id: int, current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
                       db: Session = Depends(get_db)) -> list[StatusCheckOut]:  # fmt: skip
    return [_check_out(db, c) for c in students.status_checks(db, _load(db, student_id))]


@router.post("/students/{student_id}/status-checks", response_model=StatusCheckOut, status_code=201)
def record_status_check(
    student_id: int,
    body: StatusCheckIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> StatusCheckOut:
    student = _load(db, student_id, lock=True)
    check = students.record_status_check(db, student, current.user, **body.model_dump(), ip=client_ip(request))
    db.commit()
    return _check_out(db, check)


# --- Contact details and address history (BRD FR-67) ---------------------------------------------
def _contact_out(db: Session, c) -> ContactChangeOut:
    who = db.get(User, c.recorded_by)
    return ContactChangeOut(id=c.id, effective_from=c.effective_from, address=c.address or {}, phone=c.phone,
                            email=c.email, reason=c.reason, recorded_by=who.full_name if who else None,
                            recorded_at=c.recorded_at)  # fmt: skip


@router.get("/students/{student_id}/contact", response_model=list[ContactChangeOut])
def contact_history(student_id: int, current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
                    db: Session = Depends(get_db)) -> list[ContactChangeOut]:  # fmt: skip
    return [_contact_out(db, c) for c in students.contact_history(db, _load(db, student_id))]


@router.post("/students/{student_id}/contact", response_model=StudentOut, status_code=201)
def record_contact_change(
    student_id: int,
    body: ContactIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> StudentOut:
    student = _load(db, student_id, lock=True)
    students.record_contact_change(
        db, student, current.user, effective_from=body.effective_from,
        address=body.address.model_dump(exclude_none=True), phone=body.phone, email=body.email,
        reason=body.reason, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return student_out(db, student)


# --- Notes and documents on the student record (prototype screen 12) -----------------------------
class NoteIn(StrictModel):
    body: str = Field(min_length=3, max_length=4000)


@router.get("/students/{student_id}/notes")
def list_notes(student_id: int, current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
               db: Session = Depends(get_db)) -> list[dict]:  # fmt: skip
    return students.list_notes(db, _load(db, student_id), current.user)


@router.post("/students/{student_id}/notes", status_code=201)
def add_note(student_id: int, body: NoteIn, request: Request,
             current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
             db: Session = Depends(get_db)) -> dict:  # fmt: skip
    out = students.add_note(db, _load(db, student_id), current.user, body.body, client_ip(request))
    db.commit()
    return out


@router.get("/students/{student_id}/documents")
def student_documents(student_id: int, current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
                      db: Session = Depends(get_db)) -> list[dict]:  # fmt: skip
    """Everything on the student's file: the application's documents and those added since."""
    from app.views import document_out

    docs = students.list_documents(db, _load(db, student_id), current.user)
    return [document_out(d, db).model_dump(mode="json") for d in docs]


@router.post("/students/{student_id}/documents", status_code=201)
def upload_student_document(
    student_id: int,
    request: Request,
    category: str = Form(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_roles(*STUDENT_EDIT)),
    db: Session = Depends(get_db),
) -> dict:
    from app.views import document_out

    max_bytes = int(get_setting(db, "MAX_UPLOAD_BYTES", 5_242_880))
    data = file.file.read(max_bytes + 1)
    student = _load(db, student_id)
    try:
        doc = students.add_document(db, student, current.user, category=category, filename=file.filename,
                                    data=data, max_bytes=max_bytes, ip=client_ip(request))  # fmt: skip
    except GuardFailed:
        db.commit()
        raise
    db.commit()
    return document_out(doc, db).model_dump(mode="json")


# --- The reporting queue (FR-93 to FR-100) --------------------------------------------------------
@router.get("/events", response_model=list[EventOut])
def event_queue(
    status: str | None = Query(default=None, pattern="^(pending|reported|not_required)$"),
    overdue: bool | None = Query(default=None),
    student_id: int | None = Query(default=None),
    current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
    db: Session = Depends(get_db),
) -> list[EventOut]:
    rows = events.queue(db, status=status, overdue=overdue, student_id=student_id)
    return [event_out(db, e) for e in rows]


@router.get("/events/{event_id}")
def event_detail(event_id: int, current: CurrentUser = Depends(require_roles(*STUDENT_VIEW)),
                 db: Session = Depends(get_db)) -> dict:  # fmt: skip
    """One reportable event with its evidence and its own history (prototype screen 14, "View")."""
    from app.models.application import Document
    from app.models.audit import AuditEntry

    event = events.get_event(db, event_id)
    doc = db.get(Document, event.evidence_document_id) if event.evidence_document_id else None
    uploader = db.get(User, doc.uploaded_by) if doc else None
    trail = db.scalars(select(AuditEntry).where(
        AuditEntry.entity_type == "reportable_event", AuditEntry.entity_id == event.id,
    ).order_by(AuditEntry.at))  # fmt: skip
    return {
        "event": event_out(db, event).model_dump(mode="json"),
        "evidence": {"id": doc.id, "filename": doc.filename, "uploaded_at": doc.uploaded_at,
                     "uploaded_by": uploader.full_name if uploader else None} if doc else None,
        "history": [{"at": a.at, "user_name": a.user_name, "action": a.action, "detail": a.detail} for a in trail],
    }  # fmt: skip


@router.post("/events/{event_id}/evidence", status_code=201)
def upload_event_evidence(
    event_id: int,
    request: Request,
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> dict:
    max_bytes = int(get_setting(db, "MAX_UPLOAD_BYTES", 5_242_880))
    data = file.file.read(max_bytes + 1)
    event = events.get_event(db, event_id, lock=True)
    try:
        events.attach_evidence(db, event, current.user, data=data, filename=file.filename, max_bytes=max_bytes,
                               ip=client_ip(request))  # fmt: skip
    except GuardFailed:
        db.commit()  # the quarantine row and its audit entries are evidence too (FR-22)
        raise
    db.commit()
    return event_detail(event_id, current, db)


@router.post("/events/{event_id}/report", response_model=EventOut)
def report_event(
    event_id: int,
    body: ReportIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> EventOut:
    event = events.get_event(db, event_id, lock=True)
    events.report(
        db, event, current.user, sms_reference=body.sms_reference, reported_date=body.reported_date,
        evidence_document_id=body.evidence_document_id, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return event_out(db, event)


@router.post("/events/{event_id}/not-required", response_model=EventOut)
def close_event(
    event_id: int,
    body: NotRequiredIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> EventOut:
    """Only a senior user may decide an event does not need reporting (FR-94)."""
    event = events.get_event(db, event_id)
    events.close_not_required(db, event, current.user, reason=body.reason, ip=client_ip(request))
    db.commit()
    return event_out(db, event)


# --- Metrics (FR-87 to FR-90) ---------------------------------------------------------------------
@router.get("/metrics/bca", response_model=MetricsOut)
def bca_metrics(
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    # The licence position. LLD 6 puts it with compliance and the Authorising Officer.
    current: CurrentUser = Depends(require_roles(*COMPLIANCE_ONLY)),
    db: Session = Depends(get_db),
) -> MetricsOut:
    return MetricsOut(**metrics.compute(db, period_start, period_end).to_dict())


# --- Evidence pack (FR-127, T-18) -----------------------------------------------------------------
@router.post("/applications/{application_id}/evidence-pack", response_model=ExportJobOut, status_code=202)
def request_evidence_pack(
    application_id: int,
    # A pack contains the whole file, including the payment history that LLD 6 withholds from
    # admissions and compliance, so producing one is a senior action.
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> ExportJobOut:
    """Accepted, not produced: a pack with documents takes seconds, so the client polls (LLD 10.1)."""
    app = app_svc.get_for_actor(db, application_id, current.user)
    job = ExportJob(
        id=uuid.uuid4(),
        job_type="evidence_pack",
        subject_ref=app.ref,
        application_id=app.id,
        requested_by=current.user.id,
    )
    db.add(job)
    db.commit()

    # Built inline for now: the worker queue (LLD 13.1) takes over when packs are requested in
    # bulk. The job row is committed first, so a failure here still leaves something to poll.
    try:
        evidence.run_export_job(db, job)
        db.commit()
    except Exception:  # noqa: BLE001 — the failure belongs on the job, not in a 500
        db.rollback()
        job = db.get(ExportJob, job.id)
        if job is not None and job.status not in (EXPORT_READY, "failed"):
            job.status = "failed"
            job.error_detail = "The pack could not be built. Try again, or report it with this job id."
            job.finished_at = datetime.now(UTC)
            db.commit()
    db.refresh(job)
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=ExportJobOut)
def job_status(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> ExportJobOut:
    job = db.get(ExportJob, job_id)
    if job is None or (job.requested_by != current.user.id and current.user.role != UserRole.ADMIN):
        raise NotFound()
    return _job_out(job)


@router.get("/jobs/{job_id}/download")
def job_download(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
):
    job = db.get(ExportJob, job_id)
    if job is None or job.result_storage_key is None or job.status != EXPORT_READY:
        raise NotFound()
    if job.requested_by != current.user.id and current.user.role != UserRole.ADMIN:
        raise NotFound()
    if evidence.expired(job):
        raise Conflict(
            "ERR_EXPORT_EXPIRED",
            "This export has expired. Personal data exports are deleted 24 hours after they are "
            "produced; request a new pack.",
        )
    # Every download of a pack full of personal data is audited, as an ordinary document download
    # is (LLD 11.2).
    audit.write(
        db, current.user, "data.evidence_pack", application_id=job.application_id,
        entity_type="export_job", detail=f"Evidence pack {job.id} downloaded",
    )  # fmt: skip
    db.commit()
    return StreamingResponse(
        get_storage().open(job.result_storage_key),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{job.result_filename}"',
            "Cache-Control": "no-store",
        },
    )


def _job_out(job: ExportJob) -> ExportJobOut:
    return ExportJobOut(
        id=str(job.id),
        job_type=job.job_type,
        status=job.status,
        subject_ref=job.subject_ref,
        result_filename=job.result_filename,
        expires_at=job.expires_at,
        error_detail=job.error_detail,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )

