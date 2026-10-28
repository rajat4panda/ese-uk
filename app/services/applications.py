"""Application domain logic: creation, draft saving, submission rules, duplicate detection.

No FastAPI imports (LLD 2.1). Status changes go through services.lifecycle only (LLD 7.2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import Conflict, GuardFailed, NotFound
from app.models.agent import ATTRIBUTABLE_AGENT_STATUSES, Agent
from app.models.application import Application
from app.models.config import Intake, Programme
from app.models.enums import (
    EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    ApplicationStatus,
    DocumentCategory,
    ScanStatus,
)
from app.models.user import User
from app.schemas.application import SERVER_KEYS, ApplicationData, step_for
from app.schemas.common import validation_details
from app.services import audit
from app.services.settings import get_setting

LONDON = ZoneInfo("Europe/London")
SELT_VALIDITY_YEARS = 2  # a Secure English Language Test is valid for two years


# --- 8.1 Reference generation (FR-06) ------------------------------------------------------
def next_ref(db: Session) -> str:
    n = db.execute(text("SELECT nextval('application_ref_seq')")).scalar_one()
    return f"ESE-{datetime.now(LONDON).year}-{n:06d}"


# --- Creation (FR-03) -------------------------------------------------------------------------
def open_application_for(db: Session, applicant: User) -> Application | None:
    return db.scalars(
        select(Application)
        .where(Application.applicant_id == applicant.id, Application.status.not_in(list(TERMINAL_STATUSES)))
        .order_by(Application.created_at.desc())
        .limit(1)
    ).first()


def create_or_get(db: Session, applicant: User, ip: str | None) -> tuple[Application, bool]:
    """Return the applicant's open application, creating a draft if none exists."""
    existing = open_application_for(db, applicant)
    if existing is not None:
        return existing, False
    app = Application(
        ref=next_ref(db),
        applicant_id=applicant.id,
        data={"full_name": applicant.full_name, "email": applicant.email},
    )
    db.add(app)
    db.flush()
    audit.write(db, applicant, "application.create", application=app, detail=f"Draft {app.ref} created", ip=ip)
    return app, True


# --- Draft save (FR-04, FR-10, FR-11) --------------------------------------------------------
def save_draft(
    db: Session,
    app: Application,
    actor: User,
    payload: dict[str, Any],
    *,
    if_match: datetime | None,
    ip: str | None,
) -> bool:
    """Merge validated fields into the draft. Returns True when something changed."""
    if app.status not in EDITABLE_STATUSES:
        raise Conflict(
            "ERR_NOT_EDITABLE",
            "This application has been submitted and can no longer be edited unless ESE returns it to you.",
        )
    if if_match is not None and _differs(if_match, app.updated_at):
        raise Conflict(
            "ERR_STALE_RECORD",
            "This application was changed in another window or session. Reload to see the latest version.",
        )

    try:
        parsed = ApplicationData.model_validate(payload)
    except ValidationError as exc:
        details = validation_details(exc)
        for d in details:
            d["step"] = step_for(d["field"])
        raise GuardFailed("ERR_VALIDATION", "Some answers need correcting.", details=details) from exc

    changes = parsed.model_dump(mode="json", exclude_unset=True)
    _check_references(db, changes, actor)

    new_data = dict(app.data)
    for key, value in changes.items():
        if value is None or (value == [] and key != "previous_uk_study"):
            new_data.pop(key, None)
        else:
            new_data[key] = value

    # The email on the form is the account's, shown read-only; the copy in `data` is a snapshot
    # taken when the draft was created. If the account address changes afterwards, that snapshot
    # goes stale and submission fails ERR_EMAIL_MISMATCH on a field the applicant is not allowed
    # to edit — a dead end. The account is the authority, so re-assert it on every save. A request
    # that tries to *send* a different address is still refused above, by _check_references.
    if actor.email:
        new_data["email"] = actor.email.lower()

    if new_data == app.data:
        return False

    app.data = new_data
    app.programme_id = new_data.get("programme_id")
    app.intake_id = new_data.get("intake_id")
    app.agent_id = new_data.get("agent_id")
    db.flush()
    steps = sorted({s for s in (step_for(k) for k in changes) if s})
    audit.write(
        db,
        actor,
        "application.save",
        application=app,
        detail=f"Draft saved (step {', '.join(map(str, steps)) or '-'}; {len(changes)} field(s))",
        ip=ip,
    )
    return True


def _differs(a: datetime, b: datetime) -> bool:
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    return abs((a - b).total_seconds()) > 0.000_5


def _check_references(db: Session, changes: dict[str, Any], actor: User) -> None:
    problems: list[dict] = []

    def bad(field: str, message: str, code: str = "ERR_INVALID_REFERENCE") -> None:
        problems.append({"field": field, "code": code, "message": message, "step": step_for(field)})

    if changes.get("email") and actor.email and changes["email"] != actor.email.lower():
        bad("email", "Use the email address you registered with.", "ERR_EMAIL_MISMATCH")
    if (pid := changes.get("programme_id")) is not None:
        prog = db.get(Programme, pid)
        if prog is None or prog.status != "active":
            bad("programme_id", "Select a programme from the list.")
    if (iid := changes.get("intake_id")) is not None:
        intake = db.get(Intake, iid)
        if intake is None or intake.status != "open":
            bad("intake_id", "Select an intake that is open for applications.")
        elif intake.application_deadline < date.today():
            bad("intake_id", "The application deadline for this intake has passed.", "ERR_DEADLINE_PASSED")
    if (aid := changes.get("agent_id")) is not None:
        agent = db.get(Agent, aid)
        if agent is None or agent.status not in ATTRIBUTABLE_AGENT_STATUSES:
            bad("agent_id", "Select an agent from the list, or leave this blank.")
    if problems:
        raise GuardFailed("ERR_VALIDATION", "Some answers need correcting.", details=problems)


# --- Submission rules (FR-05, Appendix A, LLD 9) ---------------------------------------------
MANDATORY: tuple[str, ...] = (
    "full_name", "date_of_birth", "email", "mobile",
    "nationality", "passport_number", "passport_issue_date", "passport_expiry_date", "passport_issuing_country",
    "home_address",
    "programme_id", "intake_id", "study_mode",
    "qualifications", "highest_level",
    "english_route",
    "been_to_uk", "previous_visa_refusal",
    "personal_statement", "study_purpose", "funding_source",
    "declaration_truthful", "declaration_data_consent", "declaration_signature_name",
)  # fmt: skip

LABELS: dict[str, str] = {
    "full_name": "Full name", "date_of_birth": "Date of birth", "email": "Email address", "mobile": "Mobile number",
    "nationality": "Nationality", "passport_number": "Passport number", "passport_issue_date": "Passport issue date",
    "passport_expiry_date": "Passport expiry date", "passport_issuing_country": "Passport issuing country",
    "home_address": "Home address", "programme_id": "Programme", "intake_id": "Intake", "study_mode": "Study mode",
    "qualifications": "Qualifications", "highest_level": "Highest level of study",
    "english_route": "English language route",
    "test_provider": "Test provider", "test_reference": "Test reference", "test_date": "Test date",
    "test_scores": "Test scores", "been_to_uk": "Whether you have been to the UK",
    "previous_uk_study": "Previous UK study", "previous_visa_refusal": "Previous visa refusal",
    "refusal_details": "Refusal details", "personal_statement": "Personal statement", "study_purpose": "Study purpose",
    "funding_source": "Funding source", "funder_details": "Funder details",
    "declaration_truthful": "Declaration of truthfulness", "declaration_data_consent": "Data protection consent",
    "declaration_signature_name": "Typed signature",
}  # fmt: skip


def _years_between(earlier: date, later: date) -> int:
    return later.year - earlier.year - ((later.month, later.day) < (earlier.month, earlier.day))


def submission_problems(db: Session, app: Application, applicant: User) -> list[dict]:
    """Every reason the application cannot be submitted, in one list (LLD 9 note)."""
    problems: list[dict] = []

    def add(field: str, code: str, message: str) -> None:
        problems.append({"field": field, "step": step_for(field), "code": code, "message": message})

    def missing(field: str, message: str | None = None) -> None:
        add(field, "ERR_REQUIRED", message or f"{LABELS.get(field, field)} is required.")

    raw = {k: v for k, v in app.data.items() if k not in SERVER_KEYS}
    try:
        d = ApplicationData.model_validate(raw)
    except ValidationError as exc:  # stored data should always be valid; report rather than crash
        for row in validation_details(exc):
            add(row["field"] or "data", row["code"], row["message"])
        return problems

    for field in MANDATORY:
        if getattr(d, field) is None:
            missing(field)

    # Nested completeness
    if d.home_address is not None:
        for part in ("line1", "city", "country"):
            if getattr(d.home_address, part) is None:
                missing(f"home_address.{part}", f"Home address: {part.replace('line1', 'first line')} is required.")
    if d.qualifications is not None:
        if not d.qualifications:
            missing("qualifications", "Add at least one qualification.")
        for i, q in enumerate(d.qualifications):
            for part in ("institution", "title", "level", "country", "start_date", "end_date", "grade"):
                if getattr(q, part) is None:
                    label = part.replace("_", " ")
                    missing(f"qualifications.{i}.{part}", f"Qualification {i + 1}: {label} is required.")
            if q.start_date and q.end_date and q.end_date < q.start_date:
                add(f"qualifications.{i}.end_date", "ERR_DATE_RANGE",
                    f"Qualification {i + 1}: end date is before start date.")

    # Conditional mandatories (Appendix A "cond")
    if d.english_route == "slt_test":
        for f in ("test_provider", "test_reference", "test_date"):
            if getattr(d, f) is None:
                missing(f, f"{LABELS[f]} is required for a Secure English Language Test.")
        scores = d.test_scores
        for part in ("overall", "reading", "writing", "listening", "speaking"):
            if scores is None or getattr(scores, part) is None:
                missing(f"test_scores.{part}", f"Test score for {part} is required.")
    if d.been_to_uk is True:
        if d.previous_uk_study is None:
            missing("previous_uk_study", "Tell us about any previous study in the UK, or confirm there was none.")
        else:
            for i, s in enumerate(d.previous_uk_study):
                for part in ("institution", "qualification_level", "start_date", "end_date", "completed"):
                    if getattr(s, part) is None:
                        label = part.replace("_", " ")
                        missing(f"previous_uk_study.{i}.{part}", f"Previous UK study {i + 1}: {label} is required.")
    if d.previous_visa_refusal is True and d.refusal_details is None:
        missing("refusal_details", "Give details of the previous visa refusal.")
    if d.funding_source is not None and d.funding_source != "self" and d.funder_details is None:
        missing("funder_details", "Tell us who is funding your studies.")

    # Declarations must be affirmative, and the typed signature must match (FR-07, Appendix A step 9)
    if d.declaration_truthful is False:
        add("declaration_truthful", "ERR_REQUIRED", "You must confirm that the information is true and complete.")
    if d.declaration_data_consent is False:
        add("declaration_data_consent", "ERR_REQUIRED", "You must consent to the processing of your data.")
    signature, name = d.declaration_signature_name, d.full_name
    if signature and name and signature.casefold() != name.casefold():
        add("declaration_signature_name", "ERR_SIGNATURE_MISMATCH", "Type your full name exactly as entered in step 1.")

    # Length rule applied at submission (drafts may be shorter)
    if d.personal_statement is not None and len(d.personal_statement) < 200:
        add("personal_statement", "ERR_TOO_SHORT", "Your personal statement must be at least 200 characters.")

    # Account email must be verified and match (Appendix A step 1)
    if d.email is not None and d.email != applicant.email.lower():
        add("email", "ERR_EMAIL_MISMATCH", "Use the email address you registered with.")
    if applicant.email_verified_at is None and get_settings().email_verification_required:
        add("email", "ERR_EMAIL_NOT_VERIFIED", "Confirm your email address using the link we sent you.")

    # References and intake-relative dates
    programme = db.get(Programme, d.programme_id) if d.programme_id else None
    if d.programme_id and (programme is None or programme.status != "active"):
        add("programme_id", "ERR_INVALID_REFERENCE", "The selected programme is no longer available.")
    intake = db.get(Intake, d.intake_id) if d.intake_id else None
    if d.intake_id and (intake is None or intake.status != "open"):
        add("intake_id", "ERR_INVALID_REFERENCE", "The selected intake is no longer open.")
    elif intake is not None and intake.application_deadline < date.today():
        add("intake_id", "ERR_DEADLINE_PASSED", "The application deadline for this intake has passed.")
    if d.agent_id:
        agent = db.get(Agent, d.agent_id)
        if agent is None or agent.status not in ATTRIBUTABLE_AGENT_STATUSES:
            add("agent_id", "ERR_INVALID_REFERENCE", "The selected agent can no longer be attributed.")

    if intake is not None:
        start = intake.start_date
        if d.date_of_birth is not None and not 15 <= _years_between(d.date_of_birth, start) <= 80:
            add("date_of_birth", "ERR_DOB_RANGE", "You must be between 15 and 80 years old at the start of the course.")
        if d.passport_expiry_date is not None and d.passport_expiry_date <= start:
            add("passport_expiry_date", "ERR_DATE_RANGE", "Your passport must be valid beyond the course start date.")
        if (
            d.english_route == "slt_test"
            and d.test_date is not None
            and _years_between(d.test_date, start) >= SELT_VALIDITY_YEARS
        ):
            add("test_date", "ERR_DATE_RANGE",
                "Your English test must still be valid (within two years) at the course start date.")
    if d.passport_issue_date and d.passport_expiry_date and d.passport_expiry_date <= d.passport_issue_date:
        add("passport_expiry_date", "ERR_DATE_RANGE", "Expiry date must be after the issue date.")

    return problems


def stamp_submission(db: Session, app: Application, actor: User, payload: dict) -> None:
    """Effect of the submit transition: server-clock timestamps and privacy notice version (FR-07, FR-133)."""
    now = datetime.now(UTC).isoformat()
    data = dict(app.data)
    data["submitted_at"] = now
    data["declaration_at"] = now
    data["privacy_notice_version"] = get_setting(db, "PRIVACY_NOTICE_VERSION", "unversioned")
    app.data = data


# --- 8.8 Duplicate detection (FR-15) -------------------------------------------------------
def find_duplicates(db: Session, app: Application) -> list[dict]:
    d = app.data
    passport = (d.get("passport_number") or "").replace(" ", "").upper() or None
    email = (d.get("email") or "").lower() or None
    dob, name = d.get("date_of_birth"), (d.get("full_name") or "").lower() or None

    clauses = []
    if passport:
        clauses.append(Application.data["passport_number"].astext == passport)
    if email:
        clauses.append(func.lower(Application.data["email"].astext) == email)
    if dob and name:
        clauses.append(
            and_(
                Application.data["date_of_birth"].astext == dob,
                func.lower(Application.data["full_name"].astext) == name,
            )
        )
    if not clauses:
        return []

    rows = db.scalars(
        select(Application)
        .where(Application.id != app.id, Application.status != ApplicationStatus.REJECTED, or_(*clauses))
        .order_by(Application.created_at)
    ).all()
    out = []
    for other in rows:
        od = other.data
        matched = []
        if passport and od.get("passport_number") == passport:
            matched.append("passport_number")
        if email and (od.get("email") or "").lower() == email:
            matched.append("email")
        if dob and name and od.get("date_of_birth") == dob and (od.get("full_name") or "").lower() == name:
            matched.append("date_of_birth+full_name")
        out.append({"id": other.id, "ref": other.ref, "status": str(other.status),
                    "full_name": od.get("full_name"), "matched_on": matched})  # fmt: skip
    return out


# --- FR-21: outstanding mandatory document categories -----------------------------------------
def required_document_categories(app: Application) -> list[str]:
    required = [DocumentCategory.PASSPORT, DocumentCategory.QUALIFICATION, DocumentCategory.FINANCIAL]
    if app.data.get("english_route") in ("slt_test", "degree_taught_in_english"):
        required.insert(2, DocumentCategory.ENGLISH)
    return [str(c) for c in required]


def outstanding_documents(app: Application) -> list[str]:
    held = {
        str(doc.category)
        for doc in app.documents
        if doc.scan_status == ScanStatus.CLEAN and doc.superseded_by is None
    }
    return [c for c in required_document_categories(app) if c not in held]


def get_for_actor(db: Session, application_id: int, actor: User, *, lock: bool = False) -> Application:
    """Load an application the actor may see. Applicants see only their own; a record outside the
    caller's scope is reported as not found, so its existence is not revealed (LLD 15, T-16).

    lock=True takes a row lock (SELECT ... FOR UPDATE) and reloads the row, so a save, submit or
    upload always sees the committed status and cannot overwrite a concurrent submission."""
    if lock:
        db.execute(select(Application.id).where(Application.id == application_id).with_for_update())
        app = db.get(Application, application_id, populate_existing=True)
    else:
        app = db.get(Application, application_id)
    if app is None:
        raise NotFound()
    if not actor.is_staff and app.applicant_id != actor.id:
        raise NotFound()
    return app
