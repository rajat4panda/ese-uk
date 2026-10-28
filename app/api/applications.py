"""Applications and documents (LLD 5.2, 5.4 audit)."""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, get_current, require_roles
from app.database import get_db
from app.errors import BadRequest, GuardFailed
from app.models.application import Application
from app.models.audit import AuditEntry
from app.models.enums import EDITABLE_STATUSES, ApplicationStatus, DocumentCategory
from app.models.enums import UserRole as R
from app.schemas.application import (
    ApplicationOut,
    ApplicationSaveRequest,
    ApplicationSummary,
    AuditEntryOut,
    DocumentOut,
    DocumentVerifyIn,
    DuplicateCandidate,
    Page,
)
from app.services import applications as svc
from app.services import documents as docs
from app.services import lifecycle, notifications
from app.services.settings import get_setting
from app.views import application_detail, document_out

router = APIRouter(tags=["applications"])

STAFF_READ = (R.ADMISSIONS, R.COMPLIANCE, R.FINANCE, R.SENIOR, R.ADMIN)  # LLD 6 pipeline read
AUDIT_READ = (R.ADMISSIONS, R.COMPLIANCE, R.SENIOR, R.ADMIN)  # LLD 6 audit trail read


def _out(app: Application, actor) -> ApplicationOut:  # noqa: ANN001 — the User behind the request
    """The applicant's own view. Staff files on the same application — interview notes, enrolment and
    reporting evidence, right-to-study scans — are never listed to the applicant (as in views.application_detail)."""
    return ApplicationOut(
        id=app.id,
        ref=app.ref,
        status=str(app.status),
        applicant_id=app.applicant_id,
        programme_id=app.programme_id,
        intake_id=app.intake_id,
        agent_id=app.agent_id,
        data=app.data,
        created_at=app.created_at,
        updated_at=app.updated_at,
        editable=app.status in EDITABLE_STATUSES,
        documents=[_doc_out(d) for d in app.documents if d.uploaded_by == actor.id],
        outstanding_documents=svc.outstanding_documents(app),
    )


_doc_out = document_out


def _summary(app: Application) -> ApplicationSummary:
    return ApplicationSummary(
        id=app.id, ref=app.ref, status=str(app.status), full_name=app.data.get("full_name"),
        email=app.data.get("email"), programme=app.programme.title if app.programme else None,
        intake=app.intake.label if app.intake else None, created_at=app.created_at, updated_at=app.updated_at,
    )  # fmt: skip


def _etag(app: Application) -> str:
    return f'"{app.updated_at.isoformat()}"'


def _parse_if_match(value: str | None) -> datetime | None:
    if not value or value.strip() == "*":
        return None
    try:
        return datetime.fromisoformat(value.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError as exc:
        raise BadRequest("ERR_BAD_REQUEST", "If-Match must carry the record's updated_at value.") from exc


# --- Applicant: create / resume -----------------------------------------------------------
@router.post("/applications", response_model=ApplicationOut, status_code=201)
def create_application(
    request: Request,
    response: Response,
    current: CurrentUser = Depends(require_roles(R.APPLICANT)),
    db: Session = Depends(get_db),
) -> ApplicationOut:
    """Returns the existing open application if present (FR-03)."""
    app, created = svc.create_or_get(db, current.user, client_ip(request))
    db.commit()
    if not created:
        response.status_code = 200
    response.headers["ETag"] = _etag(app)
    return _out(app, current.user)


@router.get("/applications/mine", response_model=list[ApplicationSummary])
def my_applications(current: CurrentUser = Depends(require_roles(R.APPLICANT)), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Application).where(Application.applicant_id == current.user.id).order_by(Application.created_at.desc())
    ).all()
    return [_summary(a) for a in rows]


# --- Staff: basic list (full pipeline with filters arrives in Sprint 2, FR-25/26) -------------
@router.get("/applications", response_model=Page)
def list_applications(
    status: list[ApplicationStatus] | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current: CurrentUser = Depends(require_roles(*STAFF_READ)),
    db: Session = Depends(get_db),
) -> Page:
    # status may be repeated, so the pipeline tabs ("Assessments", "Offers") filter on a stage —
    # a set of statuses — without the page having to guess at its own pagination (FR-25, FR-26).
    stmt = select(Application)
    if status:
        stmt = stmt.where(Application.status.in_(status))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(Application.ref).like(like),
            func.lower(Application.data["full_name"].astext).like(like),
            func.lower(Application.data["email"].astext).like(like),
        ))  # fmt: skip
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Application.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return Page(items=[_summary(a) for a in rows], total=total, page=page, page_size=page_size)


@router.get("/applications/stats")
def application_stats(
    current: CurrentUser = Depends(require_roles(*STAFF_READ)),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Count per status, for the pipeline tabs. Registered before /applications/{id} so that the
    literal path wins the match."""
    rows = db.execute(select(Application.status, func.count()).group_by(Application.status)).all()
    return {str(status): int(n) for status, n in rows}


# --- Read / save / submit ---------------------------------------------------------------------
@router.get("/applications/{application_id}")
def get_application(
    application_id: int, response: Response, current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)
) -> dict:
    """Role-shaped detail: assessments and finance are omitted for callers whose role cannot see
    them (LLD 5.9). Applicants see only their own record (T-16)."""
    app = svc.get_for_actor(db, application_id, current.user)
    response.headers["ETag"] = _etag(app)
    return application_detail(db, app, current.user)


@router.put("/applications/{application_id}", response_model=ApplicationOut)
def save_application(
    application_id: int,
    body: ApplicationSaveRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    current: CurrentUser = Depends(require_roles(R.APPLICANT)),
    db: Session = Depends(get_db),
) -> ApplicationOut:
    """Autosave target (FR-04). 409 ERR_NOT_EDITABLE unless draft or info_requested (FR-10, FR-11)."""
    app = svc.get_for_actor(db, application_id, current.user, lock=True)
    svc.save_draft(db, app, current.user, body.data, if_match=_parse_if_match(if_match), ip=client_ip(request))
    db.commit()
    response.headers["ETag"] = _etag(app)
    return _out(app, current.user)


@router.get("/applications/{application_id}/submission-check")
def submission_check(
    application_id: int, current: CurrentUser = Depends(require_roles(R.APPLICANT)), db: Session = Depends(get_db)
) -> dict:
    """What is still outstanding before submission (FR-05) — used by the review step."""
    app = svc.get_for_actor(db, application_id, current.user)
    problems = svc.submission_problems(db, app, current.user) if app.status in EDITABLE_STATUSES else []
    return {"ready": not problems, "problems": problems, "outstanding_documents": svc.outstanding_documents(app)}


@router.post("/applications/{application_id}/submit", response_model=ApplicationOut)
def submit_application(
    application_id: int,
    request: Request,
    current: CurrentUser = Depends(require_roles(R.APPLICANT)),
    db: Session = Depends(get_db),
) -> ApplicationOut:
    app = svc.get_for_actor(db, application_id, current.user, lock=True)
    lifecycle.transition(db, app, "submit", current.user, ip=client_ip(request))
    db.commit()
    # Submission confirmation with reference (FR-12) — sent after commit.
    notifications.send("application.submitted", current.user.email, {
        "name": app.data.get("full_name") or current.user.full_name, "ref": app.ref,
        "programme": app.programme.title if app.programme else "-",
        "intake": app.intake.label if app.intake else "-", "link": notifications.link("/my"),
    })  # fmt: skip
    return _out(app, current.user)


# --- Staff views ------------------------------------------------------------------------------
@router.get("/applications/{application_id}/duplicates", response_model=list[DuplicateCandidate])
def duplicates(
    application_id: int, current: CurrentUser = Depends(require_roles(*AUDIT_READ)), db: Session = Depends(get_db)
):
    app = svc.get_for_actor(db, application_id, current.user)
    return svc.find_duplicates(db, app)


@router.get("/applications/{application_id}/audit", response_model=list[AuditEntryOut])
def audit_trail(
    application_id: int, current: CurrentUser = Depends(require_roles(*AUDIT_READ)), db: Session = Depends(get_db)
):
    """Chronological audit trail (FR-124)."""
    app = svc.get_for_actor(db, application_id, current.user)
    rows = db.scalars(
        select(AuditEntry).where(AuditEntry.application_id == app.id).order_by(AuditEntry.at, AuditEntry.id)
    ).all()
    return [
        AuditEntryOut(id=e.id, at=e.at, user_name=e.user_name, user_role=e.user_role, action=e.action,
                      detail=e.detail, entity_type=e.entity_type, entity_id=e.entity_id)  # fmt: skip
        for e in rows
    ]


# --- Documents (LLD 11) -----------------------------------------------------------------------
@router.post("/applications/{application_id}/documents", response_model=DocumentOut, status_code=201)
def upload_document(
    application_id: int,
    request: Request,
    category: DocumentCategory = Form(...),
    file: UploadFile = File(...),
    expiry_date: date | None = Form(default=None),
    current: CurrentUser = Depends(get_current),
    db: Session = Depends(get_db),
) -> DocumentOut:
    max_bytes = int(get_setting(db, "MAX_UPLOAD_BYTES", 5_242_880))
    data = file.file.read(max_bytes + 1)  # sync endpoint: runs in the threadpool, off the event loop
    app = svc.get_for_actor(db, application_id, current.user, lock=True)
    try:
        doc = docs.upload(
            db, app, current.user, category=category, filename=file.filename, data=data,
            max_bytes=max_bytes, expiry_date=expiry_date, ip=client_ip(request),
        )  # fmt: skip
    except GuardFailed:
        db.commit()  # keep the quarantine row and audit entries as evidence (FR-22)
        raise
    db.commit()
    return _doc_out(doc)


@router.post("/documents/{document_id}/verify", response_model=DocumentOut)
def verify_document(
    document_id: int,
    body: DocumentVerifyIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*docs.VERIFY_ROLES)),
    db: Session = Depends(get_db),
) -> DocumentOut:
    """FR-24: mark a document as checked against its original, or take that back."""
    doc = docs.set_verified(db, document_id, current.user, verified=body.verified, ip=client_ip(request))
    db.commit()
    return _doc_out(doc, db)


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: int, request: Request, current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)
):
    doc, stream = docs.open_for_download(db, document_id, current.user, client_ip(request))
    db.commit()  # document.view is written before any byte is served
    ascii_name = doc.filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "document"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(doc.filename)}"
    return StreamingResponse(
        stream,
        media_type=doc.content_type,
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )
