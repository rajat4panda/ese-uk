"""Workflow actions: academic assessment, offers, compliance review, pre-CAS, CAS, finance
(LLD 5.3, 5.4). Every state change goes through services.lifecycle.transition()."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import CurrentUser, client_ip, get_current, require_roles
from app.database import get_db
from app.errors import ApiError, Conflict, Forbidden, NotFound
from app.models.application import Application
from app.models.cas import CAS_ASSIGNED, Cas, CasAllocation
from app.models.enums import ApplicationStatus, UserRole
from app.models.offer import Offer
from app.schemas.workflow import (
    AcademicIn,
    AllocationIn,
    AllocationOut,
    AssessmentOut,
    CasIn,
    CasOut,
    CasRegisterOut,
    ComplianceIn,
    ConditionIn,
    FeePositionOut,
    IssueIn,
    OfferIn,
    OfferOut,
    OfferResponseIn,
    PaymentIn,
    PaymentOut,
    ReasonIn,
    SecondLineIn,
)
from app.services import applications as app_svc
from app.services import assessments, audit, finance, lifecycle, notifications, offers
from app.services import cas as cas_svc
from app.services.storage import get_storage
from app.views import assessment_out, cas_out, fee_position_out, offer_out

router = APIRouter(tags=["workflow"])

ADMISSIONS = (UserRole.ADMISSIONS, UserRole.SENIOR, UserRole.ADMIN)
COMPLIANCE = (UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN)
SENIOR = (UserRole.SENIOR, UserRole.ADMIN)
FINANCE = (UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN)
STAFF = (UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN)


def _load(db: Session, application_id: int, current: CurrentUser) -> Application:
    return app_svc.get_for_actor(db, application_id, current.user, lock=True)


# --- Academic assessment (FR-27 to FR-34) ------------------------------------------------------
@router.post("/applications/{application_id}/academic", response_model=AssessmentOut)
def academic(
    application_id: int,
    body: AcademicIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS)),
    db: Session = Depends(get_db),
) -> AssessmentOut:
    """Opens the assessment on first use, saves it, and completes it when complete=true."""
    app = _load(db, application_id, current)
    ip = client_ip(request)
    if app.status == ApplicationStatus.SUBMITTED:
        lifecycle.transition(db, app, "begin_academic", current.user, ip=ip)
    assessments.save_academic(
        db, app, current.user, checklist=body.checklist, outcome=body.outcome, comments=body.comments, ip=ip,
        # Passing the set of fields the request carried lets a value be cleared: "not sent" and
        # "sent as null" mean different things, and the screen sends null to remove a wrong entry.
        english_evidence=body.english_evidence, english_standard=body.english_standard,
        english_fields_sent=body.model_fields_set & {"english_evidence", "english_standard"},
        interview={f: getattr(body, f) for f in body.model_fields_set
                   & {"interview_date", "interviewer", "interview_outcome", "interview_notes"}},
    )  # fmt: skip
    if body.complete:
        action = {"recommend_offer": "complete_academic", "request_info": "request_info", "reject": "reject"}[
            body.outcome or "recommend_offer"
        ]
        try:
            lifecycle.transition(db, app, action, current.user, {"reason": body.comments}, ip=ip)
        except ApiError:
            db.commit()  # the saved assessment survives a failed completion (T-04 pattern)
            raise
    db.commit()
    return assessment_out(db, app, "academic")


# --- Offer (FR-35 to FR-42) --------------------------------------------------------------------
@router.post("/applications/{application_id}/offer", response_model=OfferOut, status_code=201)
def create_offer(
    application_id: int,
    body: OfferIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS)),
    db: Session = Depends(get_db),
) -> OfferOut:
    app = _load(db, application_id, current)
    payload = body.model_dump()
    try:
        lifecycle.transition(db, app, "recommend_offer", current.user, payload, ip=client_ip(request))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("ERR_INVALID_STATE", "An offer already exists for this application.") from exc
    return offer_out(payload["_offer"])


@router.post("/offers/{offer_id}/issue", response_model=OfferOut)
def issue_offer(
    offer_id: int,
    body: IssueIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS)),
    db: Session = Depends(get_db),
) -> OfferOut:
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise NotFound()
    app = app_svc.get_for_actor(db, offer.application_id, current.user)
    offers.issue(db, offer, app, current.user, body.issue_method, client_ip(request))
    db.commit()
    # FR-11: the applicant is told their offer is ready. Sent after commit, so a mail failure
    # cannot roll back an offer that has been issued.
    conditions = [c.description for c in offer.conditions] if offer.conditions else []
    notifications.send("offer.issued", app.applicant.email, {
        "name": app.data.get("full_name") or app.applicant.full_name,
        "ref": app.ref,
        "offer_type": offer.offer_type,
        "programme": app.programme.title if app.programme else "-",
        "intake": app.intake.label if app.intake else "-",
        "conditions": ("This offer is conditional on:\n- " + "\n- ".join(conditions) + "\n")
                      if conditions else "",
        "link": notifications.link("/my/offer"),
    })  # fmt: skip
    return offer_out(offer)


@router.patch("/offers/{offer_id}/conditions/{condition_id}", response_model=OfferOut)
def set_condition(
    offer_id: int,
    condition_id: int,
    body: ConditionIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS, UserRole.COMPLIANCE)),
    db: Session = Depends(get_db),
) -> OfferOut:
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise NotFound()
    app = app_svc.get_for_actor(db, offer.application_id, current.user)
    offers.set_condition(db, offer, app, current.user, condition_id, status=body.status,
                         evidence_document_id=body.evidence_document_id, ip=client_ip(request))  # fmt: skip
    db.commit()
    return offer_out(offer)


@router.post("/offers/{offer_id}/override", response_model=OfferOut)
def override_conditions(
    offer_id: int,
    body: ReasonIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> OfferOut:
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise NotFound()
    app = app_svc.get_for_actor(db, offer.application_id, current.user)
    offers.override_conditions(db, offer, app, current.user, body.reason, client_ip(request))
    db.commit()
    return offer_out(offer)


@router.post("/offers/{offer_id}/response", response_model=OfferOut)
def offer_response(
    offer_id: int,
    body: OfferResponseIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(UserRole.APPLICANT, *ADMISSIONS)),
    db: Session = Depends(get_db),
) -> OfferOut:
    """The applicant accepts or declines; admissions may record a response received elsewhere (FR-39)."""
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise NotFound()
    app = app_svc.get_for_actor(db, offer.application_id, current.user, lock=True)
    if current.user.role == UserRole.APPLICANT and app.applicant_id != current.user.id:
        raise NotFound()
    action = "accept" if body.response == "accepted" else "decline"
    lifecycle.transition(db, app, action, current.user, {"response": body.response}, ip=client_ip(request))
    db.commit()
    db.refresh(offer)
    return offer_out(offer)


@router.get("/offers/{offer_id}/letter")
def offer_letter(offer_id: int, current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)):
    offer = db.get(Offer, offer_id)
    if offer is None or offer.letter_storage_key is None:
        raise NotFound()
    app = app_svc.get_for_actor(db, offer.application_id, current.user)
    if current.user.role == UserRole.FINANCE:
        raise Forbidden()
    return StreamingResponse(
        get_storage().open(offer.letter_storage_key),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="offer-{app.ref}.pdf"', "Cache-Control": "no-store"},
    )


# --- Compliance review (FR-44 to FR-52) --------------------------------------------------------
@router.post("/applications/{application_id}/compliance", response_model=AssessmentOut)
def compliance(
    application_id: int,
    body: ComplianceIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE)),
    db: Session = Depends(get_db),
) -> AssessmentOut:
    app = _load(db, application_id, current)
    ip = client_ip(request)
    if app.status == ApplicationStatus.OFFER_ACCEPTED:
        lifecycle.transition(db, app, "begin_compliance", current.user, ip=ip)
    assessments.save_compliance(
        db, app, current.user, checklist=body.checklist, risk_rating=body.risk_rating,
        atas_required=body.atas_required, atas_reference=body.atas_reference, comments=body.comments, ip=ip,
    )  # fmt: skip
    if body.complete:
        try:
            lifecycle.transition(db, app, "complete", current.user, ip=ip)
        except ApiError:
            db.commit()  # T-04, T-05: the review is kept and the application stays in compliance_review
            raise
    db.commit()
    return assessment_out(db, app, "compliance")


@router.post("/applications/{application_id}/compliance/second-line", response_model=AssessmentOut)
def second_line(
    application_id: int,
    body: SecondLineIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE)),
    db: Session = Depends(get_db),
) -> AssessmentOut:
    app = _load(db, application_id, current)
    assessments.record_second_line(db, app, current.user, body.outcome, client_ip(request))
    db.commit()
    return assessment_out(db, app, "compliance")


# --- Exception transitions ---------------------------------------------------------------------
@router.post("/applications/{application_id}/request-info")
def request_info(
    application_id: int,
    body: ReasonIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS, UserRole.COMPLIANCE)),
    db: Session = Depends(get_db),
) -> dict:
    """FR-50: returning an application requires a reason, which is sent to the applicant."""
    app = _load(db, application_id, current)
    lifecycle.transition(db, app, "request_info", current.user, {"reason": body.reason}, ip=client_ip(request))
    db.commit()
    notifications.send("application.info_requested", app.applicant.email, {
        "name": app.data.get("full_name") or app.applicant.full_name, "ref": app.ref,
        "reason": body.reason, "link": notifications.link("/apply?step=1"),
    })  # fmt: skip
    return {"status": str(app.status)}


@router.post("/applications/{application_id}/reject")
def reject(
    application_id: int,
    body: ReasonIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*ADMISSIONS)),
    db: Session = Depends(get_db),
) -> dict:
    app = _load(db, application_id, current)
    lifecycle.transition(db, app, "reject", current.user, {"reason": body.reason}, ip=client_ip(request))
    db.commit()
    return {"status": str(app.status)}


@router.post("/applications/{application_id}/withdraw")
def withdraw(
    application_id: int,
    body: ReasonIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(UserRole.APPLICANT)),
    db: Session = Depends(get_db),
) -> dict:
    """FR-14: the applicant withdraws their own application."""
    app = _load(db, application_id, current)
    lifecycle.transition(db, app, "withdraw", current.user, {"reason": body.reason}, ip=client_ip(request))
    db.commit()
    return {"status": str(app.status)}


# --- Pre-CAS summary and CAS (FR-48 to FR-61) --------------------------------------------------
@router.get("/applications/{application_id}/precas")
def precas(
    application_id: int,
    current: CurrentUser = Depends(require_roles(*COMPLIANCE, UserRole.ADMISSIONS)),
    db: Session = Depends(get_db),
) -> dict:
    app = app_svc.get_for_actor(db, application_id, current.user)
    return assessments.precas_summary(db, app, current.user)


@router.post("/applications/{application_id}/cas", response_model=CasOut, status_code=201)
def assign_cas(
    application_id: int,
    body: CasIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> CasOut:
    """The CAS guard chain (LLD 7.3). All writes share one transaction with the audit entry."""
    app = _load(db, application_id, current)
    if app.cas is not None:
        # T-11: the row lock serialises concurrent assignments; the second one sees the first's CAS.
        raise Conflict("ERR_CAS_DUPLICATE", "A CAS has already been assigned to this application.")
    payload = cas_svc.validate_payload(db, app, body.model_dump())
    try:
        lifecycle.transition(db, app, "assign_cas", current.user, payload, ip=client_ip(request))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # T-11: two concurrent assignments on one application — the unique constraint holds.
        raise Conflict("ERR_CAS_DUPLICATE", "A CAS has already been assigned to this application.") from exc
    # FR-11: the CAS number is what the applicant needs for the visa application, so it is sent
    # rather than left for them to find. After commit: a mail failure must not unassign a CAS.
    cas = payload["_cas"]
    notifications.send("cas.assigned", app.applicant.email, {
        "name": app.data.get("full_name") or app.applicant.full_name,
        "ref": app.ref,
        "cas_number": cas.cas_number,
        "course": cas.course_title,
        "start_date": cas.start_date.isoformat(),
        "end_date": cas.end_date.isoformat(),
    })  # fmt: skip
    return cas_out(cas, app)


@router.get("/cas", response_model=CasRegisterOut)
def cas_register(
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current: CurrentUser = Depends(require_roles(*STAFF)),
    db: Session = Depends(get_db),
) -> CasRegisterOut:
    """FR-59, FR-60: the CAS register with the allocation position."""
    stmt = select(Cas).join(Application, Application.id == Cas.application_id)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Cas.cas_number).like(like)
            | func.lower(Application.ref).like(like)
            | func.lower(Application.data["full_name"].astext).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Cas.assigned_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    allocation = cas_svc.current_allocation(db)
    return CasRegisterOut(
        items=[cas_out(c, db.get(Application, c.application_id)) for c in rows],
        total=total,
        allocation=AllocationOut(
            period_label=allocation.period.period_label if allocation.period else None,
            period_start=allocation.period.period_start if allocation.period else None,
            period_end=allocation.period.period_end if allocation.period else None,
            allocated=allocation.allocated, assigned=allocation.assigned, remaining=allocation.remaining,
        ),
    )


@router.get("/cas/export")
def cas_register_export(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    current: CurrentUser = Depends(require_roles(*STAFF)),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """FR-55: the whole register as CSV, so a sponsor can hand it over or reconcile it offline.

    Unpaged on purpose — an export that stopped at the first 25 rows would be worse than none,
    because the person receiving it has no way to tell it is short. The export is audited: who
    took a copy of the CAS register, and when, is itself a thing a compliance visit asks about.
    """
    stmt = select(Cas).join(Application, Application.id == Cas.application_id)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Cas.cas_number).like(like)
            | func.lower(Application.ref).like(like)
            | func.lower(Application.data["full_name"].astext).like(like)
        )
    rows = db.scalars(
        stmt.options(joinedload(Cas.authoriser), joinedload(Cas.allocation)).order_by(Cas.assigned_at.desc())
    ).all()
    apps = {
        a.id: a
        for a in db.scalars(select(Application).where(Application.id.in_([c.application_id for c in rows])))
    } if rows else {}

    def cell(value: object) -> str:
        """Neutralise a spreadsheet formula before it reaches a spreadsheet.

        Three of these columns carry text a person typed — the applicant's own name among them —
        and this file exists to be opened in Excel and handed to a third party. A cell beginning
        =, +, - or @ is evaluated on open, so a name like =HYPERLINK(...) becomes a live formula
        in the register a compliance officer is reading. Prefixing an apostrophe is what stops it;
        CSV quoting does not, because Excel strips the quoting before it evaluates the cell.
        """
        text_value = "" if value is None else str(value)
        return "'" + text_value if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "CAS number", "Application reference", "Applicant", "Course", "Start date", "End date",
        "Tuition fee", "Amount paid", "Level 1 user", "Authorised by", "Assigned at (UTC)",
        "Status", "Allocation period",
    ])  # fmt: skip
    for c in rows:
        app = apps.get(c.application_id)
        writer.writerow([cell(v) for v in (
            c.cas_number,
            app.ref if app else "",
            (app.data.get("full_name") if app else "") or "",
            c.course_title,
            c.start_date.isoformat(),
            c.end_date.isoformat(),
            f"{c.tuition_fee:.2f}",
            f"{c.amount_paid:.2f}",
            c.level1_user,
            c.authoriser.full_name if c.authoriser else "",
            c.assigned_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            c.status,
            c.allocation.period_label if c.allocation else "",
        )])  # fmt: skip

    audit.write(
        db, current.user, "cas.export", detail=f"CAS register exported: {len(rows)} row(s)"
        + (f", filtered by '{q}'" if q else ""), ip=client_ip(request),
    )  # fmt: skip
    db.commit()

    filename = f"cas-register-{date.today().isoformat()}.csv"
    return StreamingResponse(
        io.BytesIO(buffer.getvalue().encode("utf-8-sig")),  # BOM: Excel opens it as UTF-8
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/admin/allocations", response_model=AllocationOut, status_code=201)
def create_allocation(
    body: AllocationIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*SENIOR)),
    db: Session = Depends(get_db),
) -> AllocationOut:
    """FR-60: record the CAS allocation for a period. Audited as a configuration change."""
    from app.services import audit

    if body.period_end <= body.period_start:
        raise Conflict("ERR_DATE_RANGE", "The period end must be after the period start.")
    existing = db.scalars(select(CasAllocation).where(CasAllocation.period_label == body.period_label)).first()
    if existing:
        old = existing.allocated_count
        existing.allocated_count = body.allocated_count
        existing.period_start = body.period_start
        existing.period_end = body.period_end
        row = existing
        detail = (f"CAS allocation for {body.period_label} changed from {old} to {body.allocated_count} "
                  f"({body.period_start} to {body.period_end})")  # fmt: skip
    else:
        row = CasAllocation(**body.model_dump())
        db.add(row)
        detail = (f"CAS allocation for {body.period_label} recorded: {body.allocated_count} "
                  f"({body.period_start} to {body.period_end})")  # fmt: skip
    db.flush()
    audit.write(db, current.user, "admin.config_change", entity_type="cas_allocation", entity_id=row.id,
                detail=detail, ip=client_ip(request))  # fmt: skip
    db.commit()
    assigned = db.scalar(
        select(func.count()).select_from(Cas).where(Cas.allocation_period_id == row.id, Cas.status == CAS_ASSIGNED)
    ) or 0
    return AllocationOut(period_label=row.period_label, period_start=row.period_start, period_end=row.period_end,
                         allocated=row.allocated_count, assigned=int(assigned),
                         remaining=row.allocated_count - int(assigned))  # fmt: skip


# --- Finance (FR-63 to FR-69) ------------------------------------------------------------------
@router.post("/applications/{application_id}/payments", response_model=PaymentOut, status_code=201)
def record_payment(
    application_id: int,
    body: PaymentIn,
    request: Request,
    current: CurrentUser = Depends(require_roles(*FINANCE)),
    db: Session = Depends(get_db),
) -> PaymentOut:
    app = app_svc.get_for_actor(db, application_id, current.user)
    payment = finance.record_payment(
        db, app, current.user, payment_type=body.type, amount=body.amount, payment_date=body.payment_date,
        method=body.method, reference=body.reference, reason=body.reason, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return PaymentOut(id=payment.id, type=str(payment.type), amount=payment.amount,
                      payment_date=payment.payment_date, method=payment.method, reference=payment.reference,
                      reason=payment.reason, recorded_at=payment.recorded_at)  # fmt: skip


@router.get("/applications/{application_id}/fee-position", response_model=FeePositionOut)
def fee_position(
    application_id: int,
    current: CurrentUser = Depends(require_roles(*STAFF)),
    db: Session = Depends(get_db),
) -> FeePositionOut:
    app = app_svc.get_for_actor(db, application_id, current.user)
    return fee_position_out(finance.fee_position(db, app))


# --- Reference data for the staff screens ------------------------------------------------------
@router.get("/reference/checklists/{checklist_type}")
def checklist_definitions(
    checklist_type: str,
    current: CurrentUser = Depends(require_roles(*STAFF)),
    db: Session = Depends(get_db),
) -> list[dict]:
    if checklist_type not in ("academic", "compliance"):
        raise NotFound()
    return [
        {"item_key": d.item_key, "label": d.label, "mandatory": d.mandatory}
        for d in assessments.definitions(db, checklist_type)  # type: ignore[arg-type]
    ]


@router.get("/reference/offer-conditions")
def offer_condition_library(
    current: CurrentUser = Depends(require_roles(*ADMISSIONS)), db: Session = Depends(get_db)
) -> list[dict]:
    return [{"id": c.id, "description": c.description, "category": c.category} for c in offers.condition_library(db)]
