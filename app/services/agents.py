"""Recruitment agent register: adding, correcting, suspending, and the evidence held (BRD FR-90 to FR-94).

Every change is audited in the same transaction as the change itself, with the old and the new value,
because "who suspended this agent, when and why" is exactly what a compliance visit asks and the
register only shows the current state.

Status is separate from editing. Suspending or deactivating an agent is a decision with a reason; it
stops the agent being attributed new applications (FR-94, enforced where applications are saved and
submitted) but never removes the agent or its history.

Evidence — the signed agreement and the due-diligence record (FR-91) — goes through the same checks
as any other upload: size, file type by content, and the malware scan before anything is stored. A new
file supersedes the previous one rather than replacing it, so the earlier evidence remains on file.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import BadRequest, Conflict, Forbidden, GuardFailed, NotFound, ServiceUnavailable
from app.models.agent import Agent
from app.models.application import Document
from app.models.enums import DocumentCategory, ScanStatus, UserRole
from app.models.user import User
from app.schemas.agent import AgentCreateIn, AgentUpdateIn
from app.services import audit
from app.services.documents import safe_filename, sniff
from app.services.scanning import ScannerUnavailable, get_scanner
from app.services.storage import get_storage

log = logging.getLogger("ese.agents")

Opened = tuple[Document, Iterator[bytes]]

#: Read the register (LLD 6). Finance has no part in agents.
VIEW_ROLES = frozenset({UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})
#: Change the register. Agent oversight is a compliance duty; admissions reads it and uses it.
EDIT_ROLES = frozenset({UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})

EVIDENCE: dict[str, tuple[DocumentCategory, str, str]] = {
    # kind: (category, Agent column, label)
    "agreement": (DocumentCategory.AGENT_AGREEMENT, "agreement_document_id", "agreement"),
    "due_diligence": (DocumentCategory.AGENT_DUE_DILIGENCE, "due_diligence_document_id", "due-diligence record"),
}

#: The moves the register allows. Anything restricted can go back to active; a deactivated agent
#: (one ESE has stopped working with) comes back only by reactivation, not straight into review.
TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"under_review", "suspended", "deactivated"}),
    "under_review": frozenset({"active", "suspended", "deactivated"}),
    "suspended": frozenset({"active", "deactivated"}),
    "deactivated": frozenset({"active"}),
}
MIN_REASON = 10

_FIELDS = ("name", "country", "contact_name", "contact_email", "contact_phone",
           "due_diligence_date", "next_review_date", "refusal_rate_threshold_pct")  # fmt: skip


def _require_edit(actor: User) -> None:
    # Re-asserted here as well as on the route (FR-122): the service is the rule, the route a guard.
    if actor.role not in EDIT_ROLES:
        raise Forbidden(
            message="Only compliance, the Authorising Officer or an administrator can change the agent register."
        )


def get(db: Session, agent_id: int, *, lock: bool = False) -> Agent:
    stmt = select(Agent).where(Agent.id == agent_id)
    if lock:
        stmt = stmt.with_for_update()
    agent = db.scalars(stmt).first()
    if agent is None:
        raise NotFound(message="No such agent.")
    return agent


_EXISTS = "An agent with this name is already on the register for that country."


def _exists_error() -> BadRequest:
    return BadRequest("ERR_AGENT_EXISTS", _EXISTS,
                      details=[{"field": "name", "code": "ERR_AGENT_EXISTS", "message": _EXISTS}])  # fmt: skip


def _duplicate(db: Session, name: str, country: str, exclude_id: int | None = None) -> None:
    stmt = select(Agent.id).where(func.lower(Agent.name) == name.lower(), Agent.country == country)
    if exclude_id is not None:
        stmt = stmt.where(Agent.id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise _exists_error()


def _flush_unique(db: Session, obj: object | None = None) -> None:
    """Flush in a savepoint so that two simultaneous requests losing the race on the unique index
    get the same answer as the up-front check, rather than a 500."""
    try:
        with db.begin_nested():
            if obj is not None:
                db.add(obj)
            db.flush()
    except IntegrityError as exc:
        if "ux_agents_name_country" in str(exc.orig):
            raise _exists_error() from exc
        raise


def _show(value: object) -> str:
    return "—" if value is None or value == "" else str(value)


def create(db: Session, actor: User, body: AgentCreateIn, ip: str | None) -> Agent:
    _require_edit(actor)
    _duplicate(db, body.name, body.country)
    agent = Agent(**{f: getattr(body, f) for f in _FIELDS}, status="active")
    _flush_unique(db, agent)
    audit.write(db, actor, "agent.create", entity_type="agent", entity_id=agent.id,
                detail=f"Added agent {agent.name} ({agent.country})", ip=ip)  # fmt: skip
    return agent


def update(db: Session, actor: User, agent_id: int, body: AgentUpdateIn, ip: str | None) -> Agent:
    _require_edit(actor)
    agent = get(db, agent_id, lock=True)
    sent = body.model_dump(exclude_unset=True)
    for required in ("name", "country"):
        if required in sent and sent[required] is None:
            raise BadRequest("ERR_REQUIRED", "This cannot be left blank.",
                             details=[{"field": required, "code": "ERR_REQUIRED",
                                       "message": "This cannot be left blank."}])  # fmt: skip

    dd = sent.get("due_diligence_date", agent.due_diligence_date)
    review = sent.get("next_review_date", agent.next_review_date)
    if dd is not None and review is not None and review <= dd:
        # 422, like the same rule when both dates arrive in one request (schemas/agent.py).
        raise BadRequest("ERR_INVALID_DATE", "The next review must fall after the due-diligence date.", status_code=422,
                         details=[{"field": "next_review_date", "code": "ERR_INVALID_DATE",
                                   "message": "The next review must fall after the due-diligence date."}])  # fmt: skip

    name, country = sent.get("name", agent.name), sent.get("country", agent.country)
    if "name" in sent or "country" in sent:
        _duplicate(db, name, country, exclude_id=agent.id)

    changes = []
    for field, new in sent.items():
        old = getattr(agent, field)
        if old != new:
            changes.append(f"{field}: {_show(old)} -> {_show(new)}")
            setattr(agent, field, new)
    if not changes:
        return agent
    if "name" in sent or "country" in sent:
        _flush_unique(db)
    audit.write(db, actor, "agent.update", entity_type="agent", entity_id=agent.id,
                detail=f"{agent.name}: " + "; ".join(changes), ip=ip)  # fmt: skip
    return agent


def set_status(db: Session, actor: User, agent_id: int, status: str, reason: str | None, ip: str | None) -> Agent:
    _require_edit(actor)
    agent = get(db, agent_id, lock=True)
    if agent.status == status:
        raise Conflict("ERR_INVALID_STATE", f"This agent is already {status.replace('_', ' ')}.")
    if status not in TRANSITIONS.get(agent.status, frozenset()):
        raise Conflict("ERR_INVALID_STATE",
                       f"A {agent.status.replace('_', ' ')} agent cannot be moved straight to "
                       f"{status.replace('_', ' ')}.")  # fmt: skip
    reason = (reason or "").strip() or None
    if status != "active" and (reason is None or len(reason) < MIN_REASON):
        # Also enforced by the request schema; restated here because the service is the rule.
        raise BadRequest("ERR_REASON_REQUIRED", "Give a reason of at least 10 characters.", status_code=422,
                         details=[{"field": "reason", "code": "ERR_REASON_REQUIRED",
                                   "message": "Give a reason of at least 10 characters."}])  # fmt: skip
    old = agent.status
    agent.status = status
    agent.status_reason = None if status == "active" else reason
    audit.write(db, actor, "agent.status_change", entity_type="agent", entity_id=agent.id,
                detail=f"{agent.name}: status {old} -> {status}" + (f". Reason: {reason}" if reason else ""),
                ip=ip)  # fmt: skip
    return agent


def evidence_documents(db: Session, agent: Agent) -> dict[str, Document | None]:
    out: dict[str, Document | None] = {}
    for kind, (_category, column, _label) in EVIDENCE.items():
        doc_id = getattr(agent, column)
        out[kind] = db.get(Document, doc_id) if doc_id else None
    return out


def upload_evidence(
    db: Session, actor: User, agent_id: int, *, kind: str, filename: str | None, data: bytes, max_bytes: int,
    ip: str | None = None,
) -> Document:
    """Store the agreement or due-diligence record. The caller commits on GuardFailed as well, so a
    quarantined upload leaves its row and audit entries behind as evidence (FR-22)."""
    _require_edit(actor)
    if kind not in EVIDENCE:
        raise BadRequest("ERR_INVALID_FORMAT", "Choose the agreement or the due-diligence record.")
    category, column, label = EVIDENCE[kind]
    agent = get(db, agent_id, lock=True)

    if len(data) > max_bytes:
        raise BadRequest("ERR_FILE_SIZE", f"Files must be {max_bytes // (1024 * 1024)}MB or smaller.",
                         details=[{"field": "file", "code": "ERR_FILE_SIZE"}])  # fmt: skip
    if not data:
        raise BadRequest("ERR_FILE_TYPE", "The file is empty.", details=[{"field": "file", "code": "ERR_FILE_TYPE"}])
    detected = sniff(data[:512])
    if detected is None:
        raise BadRequest("ERR_FILE_TYPE", "Upload a PDF, JPG or PNG file.",
                         details=[{"field": "file", "code": "ERR_FILE_TYPE"}])  # fmt: skip
    content_type, ext = detected

    try:
        result = get_scanner().scan(data)
    except ScannerUnavailable as exc:
        log.error("Malware scanner unavailable: %s", exc)
        raise ServiceUnavailable(
            "ERR_SCAN_UNAVAILABLE", "Documents cannot be accepted right now. Please try again shortly."
        ) from exc

    display_name = safe_filename(filename)
    doc = Document(
        agent_id=agent.id,
        category=category,
        filename=display_name,
        storage_key=f"agents/{agent.id}/{category.value}/{uuid.uuid4().hex}{ext}",
        content_type=content_type,
        size_bytes=len(data),
        scan_status=ScanStatus.CLEAN.value if result.clean else ScanStatus.QUARANTINED.value,
        uploaded_by=actor.id,
    )
    db.add(doc)
    db.flush()
    audit.write(db, actor, "document.upload", entity_type="document", entity_id=doc.id,
                detail=f"Agent {agent.name} ({agent.id}) {label}: {display_name} ({content_type}, {len(data)} bytes)",
                ip=ip)  # fmt: skip

    if not result.clean:
        audit.write(db, None, "document.quarantine", entity_type="document", entity_id=doc.id,
                    detail=f"Malware scan failed ({result.signature}); file not stored", ip=ip)  # fmt: skip
        log.error("SECURITY: quarantined agent upload doc=%s agent=%s signature=%s", doc.id, agent.id, result.signature)
        raise GuardFailed("ERR_SCAN_FAILED", "This file failed the security scan and has not been accepted.",
                          details=[{"field": "file", "code": "ERR_SCAN_FAILED"}])  # fmt: skip

    get_storage().put(doc.storage_key, data, content_type)

    previous_id = getattr(agent, column)
    previous = db.get(Document, previous_id) if previous_id else None
    if previous is not None and previous.superseded_by is None:
        previous.superseded_by = doc.id
        audit.write(db, actor, "document.supersede", entity_type="document", entity_id=previous.id,
                    detail=f"Agent {agent.name} {label} {previous.filename} superseded by {display_name}",
                    ip=ip)  # fmt: skip
    setattr(agent, column, doc.id)
    # On the agent's own trail too, so "what changed on this agent" includes its evidence.
    audit.write(db, actor, "agent.update", entity_type="agent", entity_id=agent.id,
                detail=f"{agent.name}: {label}: {previous.filename if previous else '—'} -> {display_name} "
                       f"(document {doc.id})",
                ip=ip)  # fmt: skip
    return doc


def history(db: Session, agent: Agent) -> list[Document]:
    """Every file ever uploaded against this agent, newest first, superseded and quarantined included."""
    return list(db.scalars(select(Document).where(Document.agent_id == agent.id).order_by(Document.id.desc())))


def _open(db: Session, actor: User, agent: Agent, doc: Document | None, ip: str | None) -> Opened:
    if doc is None or doc.agent_id != agent.id:
        raise NotFound(message="No such file is held for this agent.")
    if doc.scan_status != ScanStatus.CLEAN:
        raise Conflict("ERR_INVALID_STATE", "This document is not available for download.")
    label = next((lbl for cat, _col, lbl in EVIDENCE.values() if cat == doc.category), str(doc.category))
    audit.write(db, actor, "document.view", entity_type="document", entity_id=doc.id,
                detail=f"Downloaded agent {agent.name} {label}: {doc.filename}"
                       + (" (superseded)" if doc.superseded_by else ""), ip=ip)  # fmt: skip
    return doc, get_storage().open(doc.storage_key)


def open_evidence(db: Session, actor: User, agent_id: int, kind: str, ip: str | None) -> Opened:
    """The current agreement or due-diligence record."""
    if actor.role not in VIEW_ROLES:
        raise Forbidden()
    if kind not in EVIDENCE:
        raise NotFound()
    agent = get(db, agent_id)
    return _open(db, actor, agent, evidence_documents(db, agent)[kind], ip)


def open_document(db: Session, actor: User, agent_id: int, document_id: int, ip: str | None) -> Opened:
    """Any file held against the agent, including superseded ones, so earlier evidence stays readable."""
    if actor.role not in VIEW_ROLES:
        raise Forbidden()
    agent = get(db, agent_id)
    return _open(db, actor, agent, db.get(Document, document_id), ip)


def reviews_due(db: Session, horizon: date) -> list[Agent]:
    """Agents still able to take applications whose next review has passed or falls by horizon."""
    return list(db.scalars(
        select(Agent).where(
            Agent.status.in_(("active", "under_review")),
            Agent.next_review_date.isnot(None),
            Agent.next_review_date <= horizon,
        ).order_by(Agent.next_review_date)
    ))  # fmt: skip
