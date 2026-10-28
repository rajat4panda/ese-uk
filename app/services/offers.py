"""Offers, conditions and the offer letter (LLD 3.5, 5.3; FR-35 to FR-42)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, GuardFailed, NotFound
from app.models.application import Application
from app.models.config import OfferConditionLibrary
from app.models.enums import UserRole
from app.models.offer import CONDITION_MET, CONDITION_OUTSTANDING, Offer, OfferCondition
from app.models.user import User
from app.services import audit, pdf
from app.services.finance import attach_fee, fee_position
from app.services.storage import get_storage

SENIOR_ROLES = frozenset({UserRole.SENIOR, UserRole.ADMIN})
ISSUE_METHODS = ("email", "portal", "post")


def condition_library(db: Session) -> list[OfferConditionLibrary]:
    return list(
        db.scalars(
            select(OfferConditionLibrary)
            .where(OfferConditionLibrary.active.is_(True))
            .order_by(OfferConditionLibrary.category, OfferConditionLibrary.id)
        )
    )


def create_offer(db: Session, app: Application, actor: User, payload: dict) -> Offer:
    """Effect of the recommend_offer transition (LLD 7.1): builds the offer from the posted payload."""
    offer_type = payload.get("offer_type")
    if offer_type not in ("conditional", "unconditional"):
        raise GuardFailed("ERR_VALIDATION", "Choose a conditional or unconditional offer.",
                          details=[{"field": "offer_type", "code": "ERR_INVALID_OPTION"}])  # fmt: skip
    conditions: list[str] = [c.strip() for c in payload.get("conditions", []) if c and c.strip()]
    if offer_type == "conditional" and not conditions:
        raise GuardFailed("ERR_VALIDATION", "A conditional offer needs at least one condition.",
                          details=[{"field": "conditions", "code": "ERR_REQUIRED"}])  # fmt: skip
    if offer_type == "unconditional" and conditions:
        raise GuardFailed("ERR_VALIDATION", "An unconditional offer cannot carry conditions.",
                          details=[{"field": "conditions", "code": "ERR_INVALID_OPTION"}])  # fmt: skip
    expiry: date | None = payload.get("expiry_date")
    if expiry is not None and expiry <= date.today():
        raise GuardFailed("ERR_DATE_RANGE", "The offer expiry date must be in the future.",
                          details=[{"field": "expiry_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip

    offer = Offer(application_id=app.id, offer_type=offer_type, expiry_date=expiry)
    db.add(offer)
    db.flush()
    for description in conditions:
        db.add(OfferCondition(offer_id=offer.id, description=description[:1000]))
    db.flush()
    attach_fee(db, app, actor)  # fix the fee schedule that applies, at offer time (LLD 3.9)
    offer.letter_storage_key = _write_letter(db, app, offer)
    db.flush()
    payload["_offer"] = offer
    app.offer = offer
    return offer


def issue(db: Session, offer: Offer, app: Application, actor: User, issue_method: str, ip: str | None) -> Offer:
    if issue_method not in ISSUE_METHODS:
        raise GuardFailed("ERR_VALIDATION", "Choose how the offer was issued.",
                          details=[{"field": "issue_method", "code": "ERR_INVALID_OPTION"}])  # fmt: skip
    if offer.issued_at is not None:
        raise Conflict("ERR_INVALID_STATE", "This offer has already been issued.")
    offer.issued_at = datetime.now(UTC)
    offer.issued_by = actor.id
    offer.issue_method = issue_method
    db.flush()
    audit.write(db, actor, "offer.issue", application=app, entity_type="offer", entity_id=offer.id,
                detail=f"Offer issued by {issue_method}; letter retained", ip=ip)  # fmt: skip
    return offer


def set_condition(
    db: Session,
    offer: Offer,
    app: Application,
    actor: User,
    condition_id: int,
    *,
    status: str,
    evidence_document_id: int | None,
    ip: str | None,
) -> OfferCondition:
    condition = next((c for c in offer.conditions if c.id == condition_id), None)
    if condition is None:
        raise NotFound()
    if status not in (CONDITION_MET, CONDITION_OUTSTANDING):
        raise GuardFailed("ERR_VALIDATION", "A condition is either met or outstanding.",
                          details=[{"field": "status", "code": "ERR_INVALID_OPTION"}])  # fmt: skip
    condition.status = status
    condition.evidence_document_id = evidence_document_id
    condition.verified_by = actor.id if status == CONDITION_MET else None
    condition.verified_at = datetime.now(UTC) if status == CONDITION_MET else None
    db.flush()
    audit.write(
        db, actor, "offer.condition_met", application=app, entity_type="offer_condition",
        entity_id=condition.id, detail=f"Condition marked {status}: {condition.description}", ip=ip,
    )  # fmt: skip
    return condition


def override_conditions(db: Session, offer: Offer, app: Application, actor: User, reason: str, ip: str | None) -> Offer:
    """Senior override of outstanding conditions (FR-40, LLD 3.5)."""
    if actor.role not in SENIOR_ROLES:
        raise GuardFailed(
            "ERR_ROLE_FORBIDDEN", "Only a senior user may override outstanding conditions.", status_code=403
        )
    if not reason or len(reason.strip()) < 10:
        raise GuardFailed("ERR_REASON_REQUIRED", "Give a reason of at least 10 characters for the override.",
                          details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}])  # fmt: skip
    offer.override_by = actor.id
    offer.override_reason = reason.strip()
    db.flush()
    audit.write(db, actor, "offer.override", application=app, entity_type="offer", entity_id=offer.id,
                detail=f"Outstanding conditions overridden: {reason.strip()}", ip=ip)  # fmt: skip
    return offer


def stamp_response(db: Session, app: Application, actor: User, payload: dict) -> None:
    """Effect of the accept and decline transitions (FR-39)."""
    offer = app.offer
    assert offer is not None
    offer.response = payload["response"]
    offer.response_at = datetime.now(UTC)
    db.flush()


# --- Offer letter (FR-37, FR-38) --------------------------------------------------------------
def _write_letter(db: Session, app: Application, offer: Offer) -> str:
    """Generate and retain the offer letter as a PDF (Sprint 3: was HTML in the R1a build)."""
    position = fee_position(db, app)
    content = pdf.offer_letter(
        ref=app.ref,
        applicant_name=str(app.data.get("full_name") or app.applicant.full_name),
        programme=app.programme.title if app.programme else "your programme",
        intake=app.intake.label if app.intake else "the next available intake",
        offer_type=offer.offer_type,
        conditions=[c.description for c in offer.conditions],
        tuition_fee=(f"{position.currency} {position.agreed_fee:,.2f}"
                     if position.agreed_fee is not None else "to be confirmed"),
        minimum_deposit=(f"{position.currency} {position.minimum_deposit:,.2f}"
                         if position.minimum_deposit is not None else "to be confirmed"),
        expiry_date=offer.expiry_date,
    )  # fmt: skip
    key = f"{app.ref}/offer/{uuid.uuid4().hex}.pdf"
    get_storage().put(key, content, "application/pdf")
    return key
