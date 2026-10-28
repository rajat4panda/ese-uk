"""Guard predicates evaluated by the lifecycle executor (LLD 7).

Each guard raises a structured error with a distinct code, so the interface can explain exactly
why an action is blocked (LLD 14.2). Guards must not write; effects write. The CAS chain
(LLD 7.3) is the most consequential path in the system and is evaluated in this order:

    role (executor) → both sign-offs → segregation of duties → conditions → deposit

Any change to segregation_of_duties or deposit_satisfied requires review by Finteam as compliance
authority (LLD 8.4).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import GuardFailed, NotFound
from app.models.application import Application
from app.models.enums import UserRole
from app.models.user import User
from app.services.applications import submission_problems
from app.services.assessments import check_academic_completable, definitions, outstanding_mandatory
from app.services.finance import fee_position
from app.services.settings import get_setting

SENIOR_ROLES = frozenset({UserRole.SENIOR, UserRole.ADMIN})


# --- Applicant ---------------------------------------------------------------------------------
def owns_application(db: Session, app: Application, actor: User, payload: dict) -> None:
    if app.applicant_id != actor.id:
        # 404 rather than 403: do not reveal that another applicant's record exists (LLD 15).
        raise NotFound()


def mandatory_fields_present(db: Session, app: Application, actor: User, payload: dict) -> None:
    problems = submission_problems(db, app, actor)
    if problems:
        raise GuardFailed(
            "ERR_MANDATORY_FIELDS",
            f"Your application is not ready to submit: {len(problems)} item(s) need attention.",
            details=problems,
        )


# --- Admissions and compliance -------------------------------------------------------------
def academic_complete(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-28: an outcome is required, and an offer cannot be recommended with checks outstanding."""
    check_academic_completable(db, app)
    row = app.academic_assessment
    if row is not None and row.outcome != "recommend_offer":
        raise GuardFailed(
            "ERR_OUTCOME_REQUIRED", "The academic assessment outcome must be 'recommend offer' to issue an offer."
        )


def academic_signed_off(db: Session, app: Application, actor: User, payload: dict) -> None:
    """An offer may only be built once the assessment is completed and signed off (FR-30, FR-35)."""
    academic_complete(db, app, actor, payload)
    row = app.academic_assessment
    if row is None or row.completed_at is None:
        raise GuardFailed(
            "ERR_SIGNOFF_INCOMPLETE", "Complete and sign off the academic assessment before issuing an offer."
        )


def all_checks_satisfied(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-45, T-04: every mandatory compliance check must be satisfied."""
    row = app.compliance_review
    checklist = row.checklist if row else {}
    missing = outstanding_mandatory(definitions(db, "compliance"), checklist)
    if missing:
        raise GuardFailed(
            "ERR_CHECKS_INCOMPLETE",
            f"{len(missing)} mandatory check(s) are outstanding: " + "; ".join(d.label for d in missing),
            details=[{"field": f"checklist.{d.item_key}", "code": "ERR_CHECKS_INCOMPLETE", "message": d.label}
                     for d in missing],  # fmt: skip
        )


def risk_rating_set(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-46, T-05."""
    row = app.compliance_review
    if row is None or row.risk_rating is None:
        raise GuardFailed("ERR_RISK_RATING_REQUIRED", "Record a compliance risk rating before completing the review.",
                          details=[{"field": "risk_rating", "code": "ERR_RISK_RATING_REQUIRED"}])  # fmt: skip


# --- CAS chain (LLD 7.3) -------------------------------------------------------------------
def both_signoffs_complete(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-54: the academic assessment and the compliance review must both be signed off."""
    missing = []
    if app.academic_assessment is None or app.academic_assessment.completed_by is None:
        missing.append("academic assessment")
    if app.compliance_review is None or app.compliance_review.completed_by is None:
        missing.append("compliance review")
    if missing:
        raise GuardFailed("ERR_SIGNOFF_INCOMPLETE",
                          "A CAS cannot be assigned until both sign-offs are recorded. Outstanding: "
                          + " and ".join(missing) + ".")  # fmt: skip


def segregation_of_duties(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-55, T-07, LLD 8.4. The single most important compliance control in the system."""
    academic = app.academic_assessment
    compliance = app.compliance_review
    conflicting = {academic.completed_by if academic else None, compliance.completed_by if compliance else None}
    if actor.id in conflicting:
        raise GuardFailed(
            "ERR_SEGREGATION_OF_DUTIES",
            "The CAS must be assigned by someone other than the user who completed the academic assessment "
            "or the compliance review.",
        )


def conditions_met(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-40, T-10: outstanding offer conditions block a CAS unless a senior user has overridden them."""
    offer = app.offer
    if offer is None:
        return
    outstanding = offer.outstanding_conditions
    if outstanding and offer.override_by is None:
        raise GuardFailed(
            "ERR_CONDITIONS_OUTSTANDING",
            f"{len(outstanding)} offer condition(s) are outstanding: "
            + "; ".join(c.description for c in outstanding),
            details=[{"field": f"condition.{c.id}", "code": "ERR_CONDITIONS_OUTSTANDING", "message": c.description}
                     for c in outstanding],  # fmt: skip
        )


def deposit_satisfied(db: Session, app: Application, actor: User, payload: dict) -> None:
    """FR-69, T-09, LLD 8.5. A senior user may override with a recorded reason, which is audited."""
    from app.services import audit

    if not get_setting(db, "DEPOSIT_GATE_ENABLED", True):
        return
    position = fee_position(db, app)
    override = (payload.get("deposit_override_reason") or "").strip()
    if override and len(override) < 10:
        raise GuardFailed(
            "ERR_REASON_REQUIRED",
            "Give a reason of at least 10 characters for overriding the deposit requirement.",
            details=[{"field": "deposit_override_reason", "code": "ERR_REASON_REQUIRED"}],
        )

    def allow_override(message: str) -> None:
        if override and actor.role in SENIOR_ROLES:
            audit.write(db, actor, "cas.deposit_override", application=app,
                        detail=f"{message} Override reason: {override}")  # fmt: skip
            return
        raise GuardFailed("ERR_DEPOSIT_NOT_MET", message,
                          details=[{"field": "deposit", "code": "ERR_DEPOSIT_NOT_MET"}])  # fmt: skip

    if not position.fee_set or position.minimum_deposit is None:
        # Fail closed: without a fee schedule the deposit cannot be evidenced (LLD 3.9).
        allow_override("No fee schedule applies to this programme and intake, so the deposit cannot be checked.")
        return
    if position.paid - position.refunded < position.minimum_deposit:
        allow_override(
            f"Deposit of {position.currency} {position.minimum_deposit} required; "
            f"{position.currency} {position.paid - position.refunded} recorded."
        )


def allocation_remaining(db: Session, app: Application, actor: User, payload: dict) -> str | None:
    """FR-61 warning: assignment beyond the recorded allocation needs a second confirmation."""
    from app.services import audit
    from app.services.cas import current_allocation

    allocation = current_allocation(db, lock=True)
    if allocation.period is None or allocation.remaining > 0:
        return None
    message = (
        f"This assignment exceeds the recorded CAS allocation for {allocation.period.period_label}: "
        f"{allocation.assigned} of {allocation.allocated} already assigned, {allocation.remaining} remaining."
    )
    if not payload.get("allocation_override_confirmed"):
        raise GuardFailed("ERR_ALLOCATION_EXCEEDED", message + " Confirm to proceed.",
                          details=[{"field": "allocation", "code": "ERR_ALLOCATION_EXCEEDED",
                                    "remaining": allocation.remaining, "allocated": allocation.allocated}])  # fmt: skip
    audit.write(db, actor, "cas.allocation_warning", application=app, detail=message + " Confirmed by the assigner.")
    return message
