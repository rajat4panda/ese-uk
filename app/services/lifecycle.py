"""Application state machine: declaration and executor (LLD 7.1, 7.2).

Transitions are data. The executor looks up the transition, checks the role, evaluates guards in
order, evaluates warnings, then applies effects, writes the new state and the audit entry in one
transaction (a savepoint inside the request transaction). No other code path writes
applications.status: the model's attribute listener refuses any write made outside this executor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, GuardFailed
from app.models.application import Application, _executor_active
from app.models.enums import ApplicationStatus as S
from app.models.enums import UserRole as R
from app.models.user import User
from app.services import assessments, audit, cas, offers
from app.services.applications import find_duplicates, stamp_submission
from app.services.guards import (
    academic_complete,
    academic_signed_off,
    all_checks_satisfied,
    allocation_remaining,
    both_signoffs_complete,
    conditions_met,
    deposit_satisfied,
    mandatory_fields_present,
    owns_application,
    risk_rating_set,
    segregation_of_duties,
)

Guard = Callable[[Session, Application, User, dict], None]
Warning_ = Callable[[Session, Application, User, dict], str | None]
Effect = Callable[[Session, Application, User, dict], None]

STAFF_REVIEWERS = frozenset({R.ADMISSIONS, R.COMPLIANCE, R.SENIOR, R.ADMIN})


@dataclass(frozen=True)
class Transition:
    to: S
    roles: frozenset[R]
    audit_action: str
    guards: tuple[Guard, ...] = ()
    warnings: tuple[Warning_, ...] = ()
    effects: tuple[Effect, ...] = ()
    describe: Callable[[Session, Application, dict], str] = field(default=lambda db, app, payload: "")


# --- Descriptions written into the audit trail ------------------------------------------------
def _describe_submit(db: Session, app: Application, payload: dict) -> str:
    dupes = find_duplicates(db, app)
    note = ""
    if dupes:
        note = f"; {len(dupes)} possible duplicate(s) flagged for review: " + ", ".join(d["ref"] for d in dupes)
    return f"Application {app.ref} submitted with declaration{note}"


def _describe_academic(db: Session, app: Application, payload: dict) -> str:
    row = app.academic_assessment
    return f"Academic assessment completed: {row.outcome if row else 'unknown'}"


def _describe_offer(db: Session, app: Application, payload: dict) -> str:
    offer = app.offer
    conditions = len(offer.conditions) if offer else 0
    return (f"{offer.offer_type.title() if offer else 'Offer'} offer created with {conditions} condition(s)"
            + (f", expires {offer.expiry_date}" if offer and offer.expiry_date else ""))  # fmt: skip


def _describe_compliance(db: Session, app: Application, payload: dict) -> str:
    row = app.compliance_review
    atas = ""
    if row is not None and row.atas_required is not None:
        atas = f"; ATAS {'required' if row.atas_required else 'not required'}"
    return f"Compliance review completed; risk rating {row.risk_rating if row else 'unknown'}{atas}"


def _reason(payload: dict) -> str:
    return (payload.get("reason") or "").strip()


def _describe_info_request(db: Session, app: Application, payload: dict) -> str:
    return f"Returned to the applicant for further information: {_reason(payload)}"


# --- Effects ----------------------------------------------------------------------------------
def _start_academic(db: Session, app: Application, actor: User, payload: dict) -> None:
    assessments.ensure_started(db, app, "academic", actor)


def _start_compliance(db: Session, app: Application, actor: User, payload: dict) -> None:
    assessments.ensure_started(db, app, "compliance", actor)


def _record_info_request(db: Session, app: Application, actor: User, payload: dict) -> None:
    reason = _reason(payload)
    if len(reason) < 10:
        raise GuardFailed("ERR_REASON_REQUIRED",
                          "Give the applicant a reason of at least 10 characters for the return.",
                          details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}])  # fmt: skip
    # The reason is shown on the applicant's status page (FR-50, LLD 14.2). Server-set key.
    app.data = {**app.data, "info_request_reason": reason}


def _clear_info_request(db: Session, app: Application, actor: User, payload: dict) -> None:
    if "info_request_reason" in app.data:
        data = dict(app.data)
        data.pop("info_request_reason")
        app.data = data


def _record_rejection(db: Session, app: Application, actor: User, payload: dict) -> None:
    reason = _reason(payload)
    if len(reason) < 10:
        raise GuardFailed("ERR_REASON_REQUIRED", "Give a reason of at least 10 characters for the rejection.",
                          details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}])  # fmt: skip
    app.data = {**app.data, "rejection_reason": reason}


def _record_withdrawal(db: Session, app: Application, actor: User, payload: dict) -> None:
    app.withdrawn_reason = _reason(payload) or "Withdrawn by the applicant"


# --- The table (LLD 7.1) ------------------------------------------------------------------
_WITHDRAWABLE = (S.DRAFT, S.SUBMITTED, S.ACADEMIC_REVIEW, S.OFFER_PENDING, S.INFO_REQUESTED,
                 S.OFFER_ISSUED, S.OFFER_ACCEPTED)  # fmt: skip

TRANSITIONS: dict[tuple[S, str], Transition] = {
    (S.DRAFT, "submit"): Transition(
        to=S.SUBMITTED, roles=frozenset({R.APPLICANT}), audit_action="application.submit",
        guards=(owns_application, mandatory_fields_present), effects=(stamp_submission,),
        describe=_describe_submit,
    ),
    (S.INFO_REQUESTED, "submit"): Transition(  # re-submission after a request for information (FR-11)
        to=S.SUBMITTED, roles=frozenset({R.APPLICANT}), audit_action="application.submit",
        guards=(owns_application, mandatory_fields_present), effects=(stamp_submission, _clear_info_request),
        describe=lambda db, app, payload: f"Application {app.ref} re-submitted after a request for information",
    ),
    (S.SUBMITTED, "begin_academic"): Transition(
        to=S.ACADEMIC_REVIEW, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="academic.save",
        effects=(_start_academic,),
        describe=lambda db, app, payload: "Academic review opened",
    ),
    # FR-35: the sign-off moves the application on. It is now waiting for an offer to be built,
    # which is admissions work rather than assessment work, and the pipeline says so.
    (S.ACADEMIC_REVIEW, "complete_academic"): Transition(
        to=S.OFFER_PENDING, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="academic.complete",
        guards=(academic_complete,), effects=(assessments.stamp_academic,), describe=_describe_academic,
    ),
    # Reachable only with an unfinished assessment, because completing one moves the application
    # to Offer Pending. It is kept so that attempt is refused with its reason (ERR_SIGNOFF_
    # INCOMPLETE) rather than with "you cannot do that from here", which tells nobody anything.
    (S.ACADEMIC_REVIEW, "recommend_offer"): Transition(
        to=S.OFFER_ISSUED, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="offer.create",
        guards=(academic_signed_off,),
        effects=(lambda db, app, actor, payload: offers.create_offer(db, app, actor, payload),),
        describe=_describe_offer,
    ),
    (S.OFFER_PENDING, "recommend_offer"): Transition(
        to=S.OFFER_ISSUED, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="offer.create",
        guards=(academic_signed_off,),
        effects=(lambda db, app, actor, payload: offers.create_offer(db, app, actor, payload),),
        describe=_describe_offer,
    ),
    # An approved application can still be returned or refused before the offer goes out: a
    # duplicate comes to light, a reference does not stand up.
    (S.OFFER_PENDING, "reject"): Transition(
        to=S.REJECTED, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="academic.complete",
        effects=(_record_rejection,),
        describe=lambda db, app, payload: f"Application rejected before the offer was built: {_reason(payload)}",
    ),
    (S.OFFER_PENDING, "request_info"): Transition(
        to=S.INFO_REQUESTED, roles=STAFF_REVIEWERS, audit_action="application.info_requested",
        effects=(_record_info_request,), describe=_describe_info_request,
    ),
    (S.ACADEMIC_REVIEW, "reject"): Transition(
        to=S.REJECTED, roles=frozenset({R.ADMISSIONS, R.SENIOR, R.ADMIN}), audit_action="academic.complete",
        effects=(_record_rejection, assessments.stamp_academic),
        describe=lambda db, app, payload: f"Application rejected at academic review: {_reason(payload)}",
    ),
    (S.ACADEMIC_REVIEW, "request_info"): Transition(
        to=S.INFO_REQUESTED, roles=STAFF_REVIEWERS, audit_action="application.info_requested",
        effects=(_record_info_request,), describe=_describe_info_request,
    ),
    (S.OFFER_ISSUED, "accept"): Transition(
        to=S.OFFER_ACCEPTED, roles=frozenset({R.APPLICANT, R.ADMISSIONS, R.SENIOR, R.ADMIN}),
        audit_action="offer.response", effects=(offers.stamp_response,),
        describe=lambda db, app, payload: "Offer accepted by the applicant",
    ),
    (S.OFFER_ISSUED, "decline"): Transition(
        to=S.OFFER_DECLINED, roles=frozenset({R.APPLICANT, R.ADMISSIONS, R.SENIOR, R.ADMIN}),
        audit_action="offer.response", effects=(offers.stamp_response,),
        describe=lambda db, app, payload: "Offer declined by the applicant",
    ),
    (S.OFFER_ACCEPTED, "begin_compliance"): Transition(
        to=S.COMPLIANCE_REVIEW, roles=frozenset({R.COMPLIANCE, R.SENIOR, R.ADMIN}), audit_action="compliance.save",
        effects=(_start_compliance,), describe=lambda db, app, payload: "Compliance review opened",
    ),
    (S.COMPLIANCE_REVIEW, "complete"): Transition(
        to=S.PRECAS_READY, roles=frozenset({R.COMPLIANCE, R.SENIOR, R.ADMIN}), audit_action="compliance.complete",
        guards=(all_checks_satisfied, risk_rating_set), effects=(assessments.stamp_compliance,),
        describe=_describe_compliance,
    ),
    (S.COMPLIANCE_REVIEW, "request_info"): Transition(
        to=S.INFO_REQUESTED, roles=frozenset({R.COMPLIANCE, R.SENIOR, R.ADMIN}),
        audit_action="compliance.return", effects=(_record_info_request,), describe=_describe_info_request,
    ),
    (S.PRECAS_READY, "assign_cas"): Transition(
        to=S.CAS_ASSIGNED, roles=frozenset({R.SENIOR, R.ADMIN}), audit_action="cas.assign",
        guards=(both_signoffs_complete, segregation_of_duties, conditions_met, deposit_satisfied),
        warnings=(allocation_remaining,),
        effects=(cas.create_cas, cas.create_student_record),
        describe=cas.describe_assignment,
    ),
    **{
        (status, "withdraw"): Transition(
            to=S.WITHDRAWN, roles=frozenset({R.APPLICANT}), audit_action="application.withdraw",
            guards=(owns_application,), effects=(_record_withdrawal,),
            describe=lambda db, app, payload: f"Withdrawn by the applicant: {app.withdrawn_reason}",
        )
        for status in _WITHDRAWABLE
    },
}


def transition(
    db: Session,
    application: Application,
    action: str,
    actor: User,
    payload: dict | None = None,
    *,
    ip: str | None = None,
) -> Application:
    payload = payload if payload is not None else {}
    t = TRANSITIONS.get((application.status, action))
    if t is None:
        raise Conflict(
            "ERR_INVALID_STATE",
            f"'{action}' is not possible while the application is {application.status.replace('_', ' ')}.",
        )
    if actor.role not in t.roles:
        raise Forbidden(details=[{"field": None, "code": "ERR_ROLE_FORBIDDEN", "allowed_roles": sorted(t.roles)}])
    previous = application.status
    with db.begin_nested():
        # Guards run inside the savepoint: deposit_satisfied records an override when a senior user
        # supplies one, and that entry must share the fate of the transition it authorised (LLD 8.5).
        for guard in t.guards:
            guard(db, application, actor, payload)
        # Warnings may write their own audit entry once confirmed, so they run inside the transaction.
        notes = [note for warn in t.warnings if (note := warn(db, application, actor, payload))]
        for effect in t.effects:
            effect(db, application, actor, payload)
        token = _executor_active.set(True)
        try:
            application.status = t.to
        finally:
            _executor_active.reset(token)
        db.flush()
        detail = t.describe(db, application, payload) or f"{previous} -> {t.to}"
        if notes:
            detail += " | " + " | ".join(notes)
        audit.write(db, actor, t.audit_action, application=application, detail=detail, ip=ip)
    return application
