"""Academic assessment and compliance review (LLD 3.4, 5.3, 9; FR-27 to FR-34, FR-44 to FR-52).

Checklist keys are validated against checklist_definitions, so a client cannot invent a check or
quietly drop one (ERR_UNKNOWN_CHECK). Completion records who signed off and when — the value the
segregation-of-duties guard later depends on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, GuardFailed
from app.models.application import Application
from app.models.compliance import ACADEMIC_OUTCOMES, AcademicAssessment, ComplianceReview
from app.models.config import ChecklistDefinition
from app.models.enums import ApplicationStatus, DocumentCategory, RiskRating
from app.models.user import User
from app.services import audit

ChecklistType = Literal["academic", "compliance"]
SECOND_LINE_RATINGS = (RiskRating.MEDIUM, RiskRating.HIGH)


def definitions(db: Session, checklist_type: ChecklistType) -> list[ChecklistDefinition]:
    return list(
        db.scalars(
            select(ChecklistDefinition)
            .where(ChecklistDefinition.checklist_type == checklist_type, ChecklistDefinition.active.is_(True))
            .order_by(ChecklistDefinition.sort_order, ChecklistDefinition.id)
        )
    )


def _validate_keys(db: Session, checklist_type: ChecklistType, checklist: dict) -> list[ChecklistDefinition]:
    defs = definitions(db, checklist_type)
    known = {d.item_key for d in defs}
    unknown = sorted(set(checklist) - known)
    if unknown:
        raise GuardFailed(
            "ERR_UNKNOWN_CHECK",
            f"Unknown checklist item(s): {', '.join(unknown)}.",
            details=[{"field": f"checklist.{k}", "code": "ERR_UNKNOWN_CHECK"} for k in unknown],
        )
    return defs


def outstanding_mandatory(defs: list[ChecklistDefinition], checklist: dict) -> list[ChecklistDefinition]:
    return [d for d in defs if d.mandatory and checklist.get(d.item_key) is not True]


def _row(db: Session, app: Application, checklist_type: ChecklistType):
    if checklist_type == "academic":
        row = app.academic_assessment
        if row is None:
            row = AcademicAssessment(application_id=app.id, checklist={})
            db.add(row)
            db.flush()
            app.academic_assessment = row
        return row
    row = app.compliance_review
    if row is None:
        row = ComplianceReview(application_id=app.id, checklist={})
        db.add(row)
        db.flush()
        app.compliance_review = row
    return row


def ensure_started(db: Session, app: Application, checklist_type: ChecklistType, actor: User) -> None:
    """Effect of begin_academic / begin_compliance: the empty assessment row exists from the start."""
    _row(db, app, checklist_type)


# --- Academic (FR-27 to FR-30) ----------------------------------------------------------------
def save_academic(
    db: Session,
    app: Application,
    actor: User,
    *,
    checklist: dict,
    outcome: str | None,
    comments: str,
    ip: str | None,
    english_evidence: str | None = None,
    english_standard: str | None = None,
    english_fields_sent: set[str] | None = None,
    interview: dict | None = None,
) -> AcademicAssessment:
    if app.status != ApplicationStatus.ACADEMIC_REVIEW:
        raise Conflict("ERR_INVALID_STATE", "The academic assessment can only be edited during academic review.")
    _validate_keys(db, "academic", checklist)
    if outcome is not None and outcome not in ACADEMIC_OUTCOMES:
        raise GuardFailed("ERR_OUTCOME_REQUIRED", "Choose a valid assessment outcome.",
                          details=[{"field": "outcome", "code": "ERR_OUTCOME_REQUIRED"}])  # fmt: skip
    row = _row(db, app, "academic")
    if row.completed_at is not None:
        raise Conflict("ERR_INVALID_STATE", "This assessment has been completed and cannot be changed.")
    row.checklist = {**row.checklist, **checklist}
    row.outcome = outcome or row.outcome
    row.comments = comments if comments is not None else row.comments
    sent = english_fields_sent if english_fields_sent is not None else {
        name for name, value in (("english_evidence", english_evidence),
                                 ("english_standard", english_standard)) if value is not None  # fmt: skip
    }
    if "english_evidence" in sent:
        row.english_evidence = english_evidence
    if "english_standard" in sent:
        row.english_standard = (english_standard or "").strip() or None
    # FR-37: only the interview fields the request carried are changed, so one can be cleared.
    for field, value in (interview or {}).items():
        if field == "interview_date" and value is not None and value > date.today():
            raise GuardFailed("ERR_INVALID_DATE", "The interview date cannot be in the future.",
                              details=[{"field": "interview_date", "code": "ERR_INVALID_DATE"}])  # fmt: skip
        setattr(row, field, (value.strip() or None) if isinstance(value, str) else value)
    db.flush()
    audit.write(db, actor, "academic.save", application=app, entity_type="academic_assessment", entity_id=row.id,
                detail=f"Academic assessment saved ({sum(1 for v in row.checklist.values() if v is True)} check(s) "
                       f"satisfied; outcome {row.outcome or 'not set'})"
                       + (f"; interview {row.interview_date} by {row.interviewer}: {row.interview_outcome}"
                          if row.interview_date and row.interviewer and row.interview_outcome else ""),
                ip=ip)  # fmt: skip
    return row


def check_academic_completable(db: Session, app: Application) -> None:
    """Outcome required (FR-28, LLD 9); an offer cannot be recommended with mandatory checks outstanding."""
    row = app.academic_assessment
    if row is None or row.outcome is None:
        raise GuardFailed("ERR_OUTCOME_REQUIRED", "Record an assessment outcome before completing this stage.",
                          details=[{"field": "outcome", "code": "ERR_OUTCOME_REQUIRED"}])  # fmt: skip
    if row.outcome == "recommend_offer":
        # FR-36: the English check is mandatory, so what it found must be on the record before the
        # assessment closes. A tick with nothing behind it is not evidence.
        if row.checklist.get("english_evidence_verified") is True:
            if not row.english_evidence:
                raise GuardFailed(
                    "ERR_ENGLISH_EVIDENCE_REQUIRED",
                    "Record which English language evidence was accepted before completing the assessment.",
                    details=[{"field": "english_evidence", "code": "ERR_ENGLISH_EVIDENCE_REQUIRED"}],
                )
            # A test or an internal assessment is judged against a threshold, and that threshold is
            # what FR-36 means by "the accepted standard". Nationality and a degree taught in
            # English are not: there is no score to fall short of.
            if row.english_evidence in ("slt_test", "provider_assessment") and not row.english_standard:
                raise GuardFailed(
                    "ERR_ENGLISH_STANDARD_REQUIRED",
                    "Record the standard the test was checked against, for example CEFR B2.",
                    details=[{"field": "english_standard", "code": "ERR_ENGLISH_STANDARD_REQUIRED"}],
                )
        # FR-37: an interview ticked as held must say who held it, when, and what it found; notes
        # ticked as uploaded must be on the file. Neither tick is mandatory, but neither may be empty.
        if row.checklist.get("interview_completed") is True:
            gaps = [f for f, v in (("interview_date", row.interview_date), ("interviewer", row.interviewer),
                                   ("interview_outcome", row.interview_outcome)) if not v]  # fmt: skip
            if gaps:
                raise GuardFailed(
                    "ERR_INTERVIEW_INCOMPLETE",
                    "Record the interview date, the interviewer and the outcome before completing the assessment.",
                    details=[{"field": f, "code": "ERR_INTERVIEW_INCOMPLETE"} for f in gaps],
                )
        if row.checklist.get("interview_notes_uploaded") is True and interview_note_files(app) == 0:
            raise GuardFailed(
                "ERR_INTERVIEW_NOTES_MISSING",
                "Upload the interview notes to the application's documents, or untick the check.",
                details=[{"field": "checklist.interview_notes_uploaded", "code": "ERR_INTERVIEW_NOTES_MISSING"}],
            )
        missing = outstanding_mandatory(definitions(db, "academic"), row.checklist)
        if missing:
            raise GuardFailed(
                "ERR_CHECKS_INCOMPLETE",
                f"{len(missing)} mandatory check(s) are outstanding: " + "; ".join(d.label for d in missing),
                details=[{"field": f"checklist.{d.item_key}", "code": "ERR_CHECKS_INCOMPLETE", "message": d.label}
                         for d in missing],  # fmt: skip
            )


def interview_note_files(app: Application) -> int:
    return sum(1 for d in app.documents if d.category == DocumentCategory.INTERVIEW_NOTES
               and d.scan_status == "clean" and d.superseded_by is None)  # fmt: skip


def stamp_academic(db: Session, app: Application, actor: User, payload: dict) -> None:
    row = app.academic_assessment
    assert row is not None
    row.completed_by = actor.id
    row.completed_at = datetime.now(UTC)
    db.flush()


# --- Compliance (FR-44 to FR-47) --------------------------------------------------------------
def save_compliance(
    db: Session,
    app: Application,
    actor: User,
    *,
    checklist: dict,
    risk_rating: RiskRating | None,
    atas_required: bool | None,
    atas_reference: str | None,
    comments: str,
    ip: str | None,
) -> ComplianceReview:
    if app.status != ApplicationStatus.COMPLIANCE_REVIEW:
        raise Conflict("ERR_INVALID_STATE", "The compliance review can only be edited during compliance review.")
    _validate_keys(db, "compliance", checklist)
    row = _row(db, app, "compliance")
    if row.completed_at is not None:
        raise Conflict("ERR_INVALID_STATE", "This review has been completed and cannot be changed.")
    row.checklist = {**row.checklist, **checklist}
    if risk_rating is not None:
        row.risk_rating = risk_rating
    if atas_required is not None:
        row.atas_required = atas_required
    if atas_reference is not None:
        row.atas_reference = atas_reference or None
    row.comments = comments if comments is not None else row.comments
    db.flush()
    audit.write(db, actor, "compliance.save", application=app, entity_type="compliance_review", entity_id=row.id,
                detail=f"Compliance review saved ({sum(1 for v in row.checklist.values() if v is True)} check(s) "
                       f"satisfied; risk {row.risk_rating or 'not set'})", ip=ip)  # fmt: skip
    return row


def stamp_compliance(db: Session, app: Application, actor: User, payload: dict) -> None:
    row = app.compliance_review
    assert row is not None
    row.completed_by = actor.id
    row.completed_at = datetime.now(UTC)
    db.flush()


def record_second_line(db: Session, app: Application, actor: User, outcome: str, ip: str | None) -> ComplianceReview:
    """Second-line review for Medium and High risk (FR-52)."""
    row = app.compliance_review
    if row is None or row.risk_rating not in SECOND_LINE_RATINGS:
        raise Conflict(
            "ERR_INVALID_STATE", "A second-line review applies only to Medium or High risk applications."
        )
    if row.completed_by == actor.id:
        raise GuardFailed(
            "ERR_SEGREGATION_OF_DUTIES",
            "The second-line review must be recorded by someone other than the first reviewer.",
        )
    row.second_line_by = actor.id
    row.second_line_at = datetime.now(UTC)
    row.second_line_outcome = outcome
    db.flush()
    audit.write(db, actor, "compliance.second_line", application=app, entity_type="compliance_review",
                entity_id=row.id, detail=f"Second-line review recorded: {outcome}", ip=ip)  # fmt: skip
    return row


# --- Pre-CAS summary (FR-48, FR-49) -----------------------------------------------------------
def precas_summary(db: Session, app: Application, actor: User | None = None) -> dict:
    from app.services.applications import outstanding_documents
    from app.services.cas import current_allocation
    from app.services.finance import fee_position

    academic = app.academic_assessment
    compliance = app.compliance_review
    offer = app.offer
    position = fee_position(db, app)
    outstanding_conditions = [c.description for c in offer.outstanding_conditions] if offer else []
    docs_outstanding = outstanding_documents(app)
    course_ok = bool(app.programme_id and app.intake_id)

    blockers: list[str] = []
    if academic is None or academic.completed_at is None:
        blockers.append("The academic assessment has not been completed.")
    if compliance is None or compliance.completed_at is None:
        blockers.append("The compliance review has not been completed.")
    elif compliance.risk_rating is None:
        blockers.append("No compliance risk rating has been recorded.")
    if outstanding_conditions and not (offer and offer.override_by):
        blockers.append(f"{len(outstanding_conditions)} offer condition(s) are outstanding.")
    if not position.fee_set:
        blockers.append("No fee schedule applies to this programme and intake.")
    elif not position.deposit_met:
        blockers.append(
            f"The deposit of GBP {position.minimum_deposit} has not been met "
            f"(GBP {position.paid} recorded)."
        )
    if not course_ok:
        blockers.append("Programme or intake is not set.")
    if current_allocation(db).period is None:
        blockers.append("No open CAS allocation period covers today.")

    # FR-55: whether *this* caller may assign is separate from whether the record is ready.
    assignment_reasons = list(blockers)
    if actor is not None and actor.id in {
        academic.completed_by if academic else None,
        compliance.completed_by if compliance else None,
    }:
        assignment_reasons.append(
            "You completed one of the two reviews, so someone else must assign the CAS "
            "(segregation of duties)."
        )

    return {
        "academic": {
            "complete": bool(academic and academic.completed_at),
            "outcome": academic.outcome if academic else None,
            "completed_by": academic.completer.full_name if academic and academic.completer else None,
            "completed_at": academic.completed_at if academic else None,
        },
        "compliance": {
            "complete": bool(compliance and compliance.completed_at),
            "risk_rating": str(compliance.risk_rating) if compliance and compliance.risk_rating else None,
            "atas_required": compliance.atas_required if compliance else None,
            "completed_by": compliance.completer.full_name if compliance and compliance.completer else None,
            "completed_at": compliance.completed_at if compliance else None,
            "second_line_at": compliance.second_line_at if compliance else None,
        },
        "documents": {"outstanding": docs_outstanding, "complete": not docs_outstanding},
        "conditions": {"outstanding": outstanding_conditions, "overridden": bool(offer and offer.override_by)},
        "fees": {
            "agreed_fee": position.agreed_fee,
            "minimum_deposit": position.minimum_deposit,
            "paid": position.paid,
            "outstanding": position.outstanding,
            "deposit_met": position.deposit_met,
        },
        "course": {"programme": app.programme.title if app.programme else None,
                   "intake": app.intake.label if app.intake else None,
                   "start_date": app.intake.start_date if app.intake else None},  # fmt: skip
        "assignment": {"can_assign": not assignment_reasons, "reasons": assignment_reasons},
        "ready_for_cas": app.status == ApplicationStatus.PRECAS_READY and not blockers,
        "blockers": blockers,
    }
