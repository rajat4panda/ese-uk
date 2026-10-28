"""Recruitment agent register and performance (FR-102–FR-107, LLD 3.11, prototype screen 11).

The register is maintained here too: add, correct, suspend and hold evidence (BRD FR-90 to FR-94);
the rules live in services/agents.py. Performance is derived from the applications already attributed to
each agent rather than stored, so the figures cannot drift from the records they describe: an agent
row is a view over its applications, offers, CAS and enrolments, not a second copy of them.

Refusal rate is the number for which the agent is actually monitored (FR-106/FR-107). It is left as
null below the small-denominator floor, for the same reason the BCA metrics are (LLD 8.3): a single
refusal out of three applications is noise, and showing it as 33% invites a decision it cannot carry.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, require_roles
from app.api.reference import _countries
from app.database import get_db
from app.errors import GuardFailed
from app.models.agent import Agent
from app.models.application import Application
from app.models.cas import Cas, VisaOutcomeRecord
from app.models.enums import EnrolmentStatus, UserRole, VisaOutcome
from app.models.offer import Offer
from app.models.student import StudentRecord
from app.models.user import User
from app.schemas.agent import AgentCreateIn, AgentStatusIn, AgentUpdateIn, EvidenceKind
from app.services import agents as svc
from app.services.settings import get_setting

router = APIRouter(tags=["agents"])

# Agents belong to recruitment and to compliance monitoring. Finance has no part in them (LLD 6).
AGENT_VIEW = (UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN)

#: Below this many visa decisions a refusal rate is not reported (LLD 8.3).
SMALL_DENOMINATOR = 5


def _counts(db: Session) -> dict[int, dict[str, int]]:
    """One pass per measure, keyed by agent. Applications with no agent are ignored."""
    out: dict[int, dict[str, int]] = {}

    def tally(stmt: Select, key: str) -> None:
        for agent_id, n in db.execute(stmt).all():
            if agent_id is None:
                continue
            out.setdefault(agent_id, {})[key] = int(n)

    base = select(Application.agent_id, func.count()).where(Application.agent_id.is_not(None))

    tally(base.group_by(Application.agent_id), "applications")
    tally(
        base.join(Offer, Offer.application_id == Application.id)
        .where(Offer.issued_at.is_not(None))
        .group_by(Application.agent_id),
        "offers",
    )
    tally(
        base.join(Cas, Cas.application_id == Application.id).group_by(Application.agent_id),
        "cas",
    )
    tally(
        base.join(StudentRecord, StudentRecord.application_id == Application.id)
        .where(StudentRecord.enrolment_status == EnrolmentStatus.ENROLLED)
        .group_by(Application.agent_id),
        "enrolled",
    )
    tally(
        base.join(Cas, Cas.application_id == Application.id)
        .join(VisaOutcomeRecord, VisaOutcomeRecord.cas_id == Cas.id)
        .group_by(Application.agent_id),
        "visa_decisions",
    )
    tally(
        base.join(Cas, Cas.application_id == Application.id)
        .join(VisaOutcomeRecord, VisaOutcomeRecord.cas_id == Cas.id)
        .where(VisaOutcomeRecord.outcome == VisaOutcome.REFUSED)
        .group_by(Application.agent_id),
        "refused",
    )
    return out


@router.get("/agents")
def list_agents(
    current: CurrentUser = Depends(require_roles(*AGENT_VIEW)),
    db: Session = Depends(get_db),
) -> list[dict]:
    counts = _counts(db)
    names = {c["code"]: c["name"] for c in _countries()}
    rows = db.scalars(select(Agent).order_by(Agent.name)).all()
    out: list[dict] = []
    for a in rows:
        c = counts.get(a.id, {})
        decisions = c.get("visa_decisions", 0)
        refused = c.get("refused", 0)
        threshold = a.refusal_rate_threshold_pct
        rate: float | None = None
        over = False
        if decisions >= SMALL_DENOMINATOR:
            rate = round(refused * 100 / decisions, 1)
            if threshold is not None:
                over = Decimal(str(rate)) > threshold
        out.append({
            "id": a.id,
            "name": a.name,
            "country": names.get(a.country, a.country),
            "country_code": a.country,
            "status": a.status,
            "status_reason": a.status_reason,
            "contact_name": a.contact_name,
            "contact_email": a.contact_email,
            "applications": c.get("applications", 0),
            "offers": c.get("offers", 0),
            "cas": c.get("cas", 0),
            "enrolled": c.get("enrolled", 0),
            "visa_decisions": decisions,
            "refused": refused,
            "refusal_rate_pct": rate,
            "refusal_rate_threshold_pct": float(threshold) if threshold is not None else None,
            "refusal_rate_over_threshold": over,
            "due_diligence_date": a.due_diligence_date,
            "next_review_date": a.next_review_date,
        })
    return out


# --- Maintaining the register (BRD FR-90 to FR-94) --------------------------------------------
AGENT_EDIT = tuple(svc.EDIT_ROLES)


def _detail(db: Session, a: Agent) -> dict:
    names = {c["code"]: c["name"] for c in _countries()}
    evidence = {}
    for kind, doc in svc.evidence_documents(db, a).items():
        if doc is None:
            evidence[kind] = None
            continue
        uploader = db.get(User, doc.uploaded_by)
        evidence[kind] = {"id": doc.id, "filename": doc.filename, "uploaded_at": doc.uploaded_at,
                          "uploaded_by": uploader.full_name if uploader else None,
                          "available": doc.scan_status == "clean"}  # fmt: skip
    return {
        "id": a.id,
        "name": a.name,
        "country_code": a.country,
        "country": names.get(a.country, a.country),
        "contact_name": a.contact_name,
        "contact_email": a.contact_email,
        "contact_phone": a.contact_phone,
        "status": a.status,
        "status_reason": a.status_reason,
        "due_diligence_date": a.due_diligence_date,
        "next_review_date": a.next_review_date,
        "refusal_rate_threshold_pct": float(a.refusal_rate_threshold_pct)
        if a.refusal_rate_threshold_pct is not None else None,
        "evidence": evidence,
        "evidence_history": [
            {"id": d.id, "kind": "agreement" if d.category == "agent_agreement" else "due_diligence",
             "filename": d.filename, "uploaded_at": d.uploaded_at,
             "uploaded_by": (u.full_name if (u := db.get(User, d.uploaded_by)) else None),
             "status": "quarantined" if d.scan_status != "clean" else ("superseded" if d.superseded_by else "current")}
            for d in svc.history(db, a)
        ],
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


@router.get("/agents/{agent_id}")
def get_agent(agent_id: int, current: CurrentUser = Depends(require_roles(*AGENT_VIEW)),
              db: Session = Depends(get_db)) -> dict:  # fmt: skip
    return _detail(db, svc.get(db, agent_id))


@router.post("/agents", status_code=201)
def create_agent(body: AgentCreateIn, request: Request, current: CurrentUser = Depends(require_roles(*AGENT_EDIT)),
                 db: Session = Depends(get_db)) -> dict:  # fmt: skip
    agent = svc.create(db, current.user, body, client_ip(request))
    db.commit()
    return _detail(db, agent)


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: int, body: AgentUpdateIn, request: Request,
                 current: CurrentUser = Depends(require_roles(*AGENT_EDIT)),
                 db: Session = Depends(get_db)) -> dict:  # fmt: skip
    agent = svc.update(db, current.user, agent_id, body, client_ip(request))
    db.commit()
    return _detail(db, agent)


@router.post("/agents/{agent_id}/status")
def set_agent_status(agent_id: int, body: AgentStatusIn, request: Request,
                     current: CurrentUser = Depends(require_roles(*AGENT_EDIT)),
                 db: Session = Depends(get_db)) -> dict:  # fmt: skip
    agent = svc.set_status(db, current.user, agent_id, body.status, body.reason, client_ip(request))
    db.commit()
    return _detail(db, agent)


@router.post("/agents/{agent_id}/evidence", status_code=201)
def upload_agent_evidence(
    agent_id: int,
    request: Request,
    kind: EvidenceKind = Form(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_roles(*AGENT_EDIT)),
    db: Session = Depends(get_db),
) -> dict:
    max_bytes = int(get_setting(db, "MAX_UPLOAD_BYTES", 5_242_880))
    data = file.file.read(max_bytes + 1)
    try:
        svc.upload_evidence(db, current.user, agent_id, kind=kind, filename=file.filename, data=data,
                            max_bytes=max_bytes, ip=client_ip(request))  # fmt: skip
    except GuardFailed:
        db.commit()  # keep the quarantine row and audit entries as evidence (FR-22)
        raise
    db.commit()
    return _detail(db, svc.get(db, agent_id))


def _stream(doc, stream) -> StreamingResponse:  # noqa: ANN001
    ascii_name = doc.filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "document"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(doc.filename)}"
    return StreamingResponse(stream, media_type=doc.content_type,
                             headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})  # fmt: skip


@router.get("/agents/{agent_id}/evidence/{kind}")
def download_agent_evidence(agent_id: int, kind: str, request: Request,
                            current: CurrentUser = Depends(require_roles(*AGENT_VIEW)),
                            db: Session = Depends(get_db)):  # fmt: skip
    doc, stream = svc.open_evidence(db, current.user, agent_id, kind, client_ip(request))
    db.commit()  # document.view is written before any byte is served
    return _stream(doc, stream)


@router.get("/agents/{agent_id}/documents/{document_id}")
def download_agent_document(agent_id: int, document_id: int, request: Request,
                            current: CurrentUser = Depends(require_roles(*AGENT_VIEW)),
                            db: Session = Depends(get_db)):  # fmt: skip
    """Earlier evidence, superseded by a newer file but still on record (FR-91)."""
    doc, stream = svc.open_document(db, current.user, agent_id, document_id, client_ip(request))
    db.commit()
    return _stream(doc, stream)
