"""Configuration without code changes (BRD FR-105): compliance settings, programmes, intakes and fees.

Until this module, every one of these was changed with SQL against the live database, which left no
audit entry — the one thing a compliance visit would ask for about a threshold that decides when a
student is flagged. Every change made here is written to the audit trail in the same transaction,
with the old and the new value (admin.config_change; finance.schedule_change for fees).

Only the settings listed in SETTINGS can be edited, each with its type and bounds. Some rows in
system_settings are deliberately absent: CAS_ALLOCATION is managed as a dated allocation period, and
MAX_UPLOAD_BYTES and SESSION_EXPIRY_HOURS are security limits that belong in deployment config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BadRequest, Conflict, Forbidden, NotFound
from app.models.config import Intake, Programme, SystemSetting
from app.models.enums import UserRole
from app.models.finance import FeeSchedule
from app.models.user import User
from app.schemas.common import normalise_email
from app.services import audit

#: Compliance policy: the Authorising Officer and administrators.
SETTINGS_ROLES = frozenset({UserRole.SENIOR, UserRole.ADMIN})
#: Fees: finance as well, because fees are its side of the application (LLD 6).
FEE_ROLES = frozenset({UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN})


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: str
    label: str
    hint: str
    value_type: str  # int | decimal | bool | text | email
    default: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    unit: str = ""


SETTINGS: tuple[SettingDef, ...] = (
    # Reporting to UKVI
    SettingDef("REPORTING_WORKING_DAYS", "UKVI reporting", "Reporting deadline",
               "Working days allowed to report an event to UKVI. The Home Office duty is 10.", "int", "10", Decimal(1),
               Decimal(10), "working days"),
    SettingDef("ESCALATION_DAYS_BEFORE", "UKVI reporting", "Deadline warning",
               "Working days before a reporting deadline that the event is escalated.", "int", "3", Decimal(1),
               Decimal(9), "working days"),
    SettingDef("NON_ENROLMENT_GRACE_DAYS", "UKVI reporting", "Non-enrolment grace period",
               "Days after the course start before a student who has not enrolled is raised as non-enrolment.",
               "int", "14", Decimal(0), Decimal(60), "days"),
    # Attendance and engagement
    SettingDef("ATTENDANCE_THRESHOLD_PCT", "Attendance and engagement", "Attendance threshold",
               "A student below this attendance rate is flagged and the compliance inbox is alerted.",
               "int", "80", Decimal(1), Decimal(100), "%"),
    SettingDef("ENGAGEMENT_ALERT_REPEAT_DAYS", "Attendance and engagement", "Repeat an unresolved attendance alert",
               "While one breach persists, the alert is repeated no more often than this.", "int", "14", Decimal(1),
               Decimal(90), "days"),
    # Expiry and review notices
    SettingDef("PERMISSION_EXPIRY_ALERT_DAYS", "Notices", "Visa expiry notice",
               "Days before a student's permission to stay expires that it is flagged.", "int", "60", Decimal(7),
               Decimal(365), "days"),
    SettingDef("OFFER_EXPIRY_ALERT_DAYS", "Notices", "Offer expiry notice",
               "Days before an unanswered offer expires that the compliance inbox is told.", "int", "7", Decimal(1),
               Decimal(90), "days"),
    SettingDef("AGENT_REVIEW_ALERT_DAYS", "Notices", "Agent review notice",
               "Days before an agent's review date that the compliance inbox is reminded.", "int", "30", Decimal(1),
               Decimal(180), "days"),
    # Basic Compliance Assessment
    SettingDef("BCA_REFUSAL_MAX_PCT", "Basic Compliance Assessment", "Visa refusal rate — maximum",
               "UKVI's current threshold for the refusal rate. Check against the sponsor guidance in force.",
               "decimal", "5.0", Decimal(0), Decimal(100), "%"),
    SettingDef("BCA_ENROLMENT_MIN_PCT", "Basic Compliance Assessment", "Enrolment rate — minimum",
               "UKVI's current threshold for the enrolment rate.", "decimal", "95.0", Decimal(0), Decimal(100), "%"),
    SettingDef("BCA_COMPLETION_MIN_PCT", "Basic Compliance Assessment", "Course completion rate — minimum",
               "UKVI's current threshold for the completion rate.", "decimal", "85.0", Decimal(0), Decimal(100), "%"),
    SettingDef("BCA_AMBER_BAND_PCT", "Basic Compliance Assessment", "Amber band",
               "How close to a threshold a rate turns amber rather than green.", "decimal", "2.0", Decimal(0),
               Decimal(20), "percentage points"),
    # CAS
    SettingDef("DEPOSIT_GATE_ENABLED", "CAS", "Deposit required before CAS",
               "When on, a CAS cannot be assigned until the minimum deposit is paid.", "bool", "true"),
    # Contacts and notices
    SettingDef("COMPLIANCE_INBOX", "Contacts", "Compliance inbox",
               "Where compliance alerts are sent.", "email", "compliance@ese.example"),
    SettingDef("AUTHORISING_OFFICER_EMAIL", "Contacts", "Authorising Officer's email",
               "Copied on new reportable events.", "email", "authorising-officer@ese.example"),
    SettingDef("PRIVACY_NOTICE_VERSION", "Contacts", "Privacy notice version",
               "Recorded with each applicant's consent. Change it when the privacy notice changes.", "text",
               "draft-2026-09"),
)  # fmt: skip
BY_KEY = {s.key: s for s in SETTINGS}

#: Settings that switch off a control rather than tune it. Only the Authorising Officer — who
#: answers to UKVI for it — may change these; an administrator may not (LLD 6).
AO_ONLY = frozenset({"DEPOSIT_GATE_ENABLED"})
#: The escalation warning is counted back from the reporting deadline, so it must fall inside it.
_ORDERED = ("ESCALATION_DAYS_BEFORE", "REPORTING_WORKING_DAYS")


def _require(actor: User, roles: frozenset[UserRole]) -> None:
    if actor.role not in roles:
        raise Forbidden()


def _bad(field: str, message: str, code: str = "ERR_INVALID_FORMAT") -> BadRequest:
    return BadRequest(code, message, status_code=422, details=[{"field": field, "code": code, "message": message}])


# --- Settings ------------------------------------------------------------------------------------
def list_settings(db: Session, actor: User) -> list[dict]:
    _require(actor, SETTINGS_ROLES)
    rows = {r.key: r for r in db.scalars(select(SystemSetting)).all()}
    out = []
    for s in SETTINGS:
        row = rows.get(s.key)
        editor = db.get(User, row.updated_by) if row and row.updated_by else None
        out.append({
            "key": s.key, "group": s.group, "label": s.label, "hint": s.hint, "type": s.value_type,
            "unit": s.unit, "minimum": None if s.minimum is None else str(s.minimum),
            "maximum": None if s.maximum is None else str(s.maximum),
            "value": row.value if row else s.default, "is_default": row is None,
            "updated_by": editor.full_name if editor else None,
            "updated_at": row.updated_at if row and row.updated_by else None,
            "ao_only": s.key in AO_ONLY,
        })  # fmt: skip
    return out


def _normalise(s: SettingDef, raw: str) -> str:
    raw = (raw or "").strip()
    if s.value_type == "bool":
        if raw.lower() not in {"true", "false"}:
            raise _bad("value", "Choose on or off.")
        return raw.lower()
    if s.value_type == "email":
        try:
            return normalise_email(raw) or ""
        except Exception as exc:  # noqa: BLE001 — pydantic custom error from the shared helper
            raise _bad("value", "Enter a valid email address.") from exc
    if s.value_type == "text":
        if not 1 <= len(raw) <= 100:
            raise _bad("value", "Enter between 1 and 100 characters.")
        return raw
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise _bad("value", "Enter a number.") from exc
    if not number.is_finite():
        raise _bad("value", "Enter a number.")
    if (s.minimum is not None and number < s.minimum) or (s.maximum is not None and number > s.maximum):
        raise _bad("value", f"Enter a value from {s.minimum} to {s.maximum}.", "ERR_OUT_OF_RANGE")
    if s.value_type == "int":
        if number != number.to_integral_value():
            raise _bad("value", "Enter a whole number.")
        return str(int(number))  # "1e1", "10.0" and "-0" are stored as 10 and 0
    if number != number.quantize(Decimal("0.1")):
        raise _bad("value", "Use at most one decimal place.")
    return str(number.quantize(Decimal("0.1")) + Decimal("0.0"))  # "5", "5.00" and "5.0" are all 5.0


def _current(db: Session, key: str) -> str:
    row = db.get(SystemSetting, key)
    return row.value if row else BY_KEY[key].default


def _check_together(db: Session, key: str, value: str) -> None:
    if key not in _ORDERED:
        return
    current = {k: int(_current(db, k)) for k in _ORDERED}
    current[key] = int(value)
    if current["ESCALATION_DAYS_BEFORE"] >= current["REPORTING_WORKING_DAYS"]:
        raise _bad("value", "The deadline warning must be fewer working days than the reporting deadline "
                   f"({current['ESCALATION_DAYS_BEFORE']} is not less than {current['REPORTING_WORKING_DAYS']}).",
                   "ERR_OUT_OF_RANGE")  # fmt: skip


def update_setting(db: Session, actor: User, key: str, raw: str, ip: str | None) -> dict:
    _require(actor, SETTINGS_ROLES)
    s = BY_KEY.get(key)
    if s is None:
        raise NotFound(message="That setting cannot be changed here.")
    if key in AO_ONLY and actor.role != UserRole.SENIOR:
        raise Forbidden(message="Only the Authorising Officer can change this setting.")
    value = _normalise(s, raw)
    _check_together(db, key, value)
    row = db.get(SystemSetting, key, with_for_update=True)
    old = row.value if row else s.default
    if old == value:
        raise Conflict("ERR_UNCHANGED", "That is already the current value.")
    stored_type = "text" if s.value_type == "email" else s.value_type
    now = datetime.now(UTC)
    if row is None:
        row = SystemSetting(key=key, value=value, value_type=stored_type, updated_by=actor.id, updated_at=now)
        db.add(row)
    else:
        row.value, row.updated_by, row.updated_at = value, actor.id, now
    db.flush()
    audit.write(db, actor, "admin.config_change", entity_type="system_setting",
                detail=f"{key} ({s.label}): {old} -> {value}", ip=ip)  # fmt: skip
    return next(x for x in list_settings(db, actor) if x["key"] == key)


# --- Programmes and intakes ---------------------------------------------------------------------------
def _programme_out(p: Programme) -> dict:
    return {"id": p.id, "title": p.title, "qualification_level": p.qualification_level,
            "duration_months": p.duration_months, "atas_required": p.atas_required, "status": p.status}  # fmt: skip


def _intake_out(i: Intake) -> dict:
    return {"id": i.id, "label": i.label, "start_date": i.start_date,
            "application_deadline": i.application_deadline, "status": i.status}  # fmt: skip


def list_programmes(db: Session) -> list[dict]:
    return [_programme_out(p) for p in db.scalars(select(Programme).order_by(Programme.status, Programme.title))]


def list_intakes(db: Session) -> list[dict]:
    return [_intake_out(i) for i in db.scalars(select(Intake).order_by(Intake.start_date.desc()))]


def _diff(obj: object, changes: dict) -> list[str]:
    out = []
    for field, new in changes.items():
        old = getattr(obj, field)
        if old != new:
            out.append(f"{field}: {old} -> {new}")
            setattr(obj, field, new)
    return out


def save_programme(db: Session, actor: User, programme_id: int | None, fields: dict, ip: str | None) -> dict:
    _require(actor, SETTINGS_ROLES)
    if programme_id is None:
        p = Programme(**fields)
        db.add(p)
        db.flush()
        detail = f"Programme added: {p.title} ({p.qualification_level}, {p.duration_months} months)"
    else:
        p = db.get(Programme, programme_id, with_for_update=True)
        if p is None:
            raise NotFound()
        changes = _diff(p, fields)
        if not changes:
            return _programme_out(p)
        db.flush()
        detail = f"Programme {p.id}: " + "; ".join(changes)
    audit.write(db, actor, "admin.config_change", entity_type="programme", entity_id=p.id, detail=detail, ip=ip)
    return _programme_out(p)


def save_intake(db: Session, actor: User, intake_id: int | None, fields: dict, ip: str | None) -> dict:
    _require(actor, SETTINGS_ROLES)
    i = None if intake_id is None else db.get(Intake, intake_id, with_for_update=True)
    if intake_id is not None and i is None:
        raise NotFound()
    start = fields.get("start_date", i.start_date if i else None)
    deadline = fields.get("application_deadline", i.application_deadline if i else None)
    if start and deadline and deadline > start:
        raise _bad("application_deadline", "The application deadline must be on or before the start date.",
                   "ERR_INVALID_DATE")  # fmt: skip
    if i is None:
        i = Intake(**fields)
        db.add(i)
        db.flush()
        detail = f"Intake added: {i.label} (starts {i.start_date}, deadline {i.application_deadline})"
    else:
        changes = _diff(i, fields)
        if not changes:
            return _intake_out(i)
        db.flush()
        detail = f"Intake {i.id}: " + "; ".join(changes)
    audit.write(db, actor, "admin.config_change", entity_type="intake", entity_id=i.id, detail=detail, ip=ip)
    return _intake_out(i)


# --- Fees ----------------------------------------------------------------------------------------------
def list_fees(db: Session, actor: User) -> list[dict]:
    """The current schedule for every programme and intake pair, including pairs with none."""
    _require(actor, FEE_ROLES)
    current = {(f.programme_id, f.intake_id): f
               for f in db.scalars(select(FeeSchedule).where(FeeSchedule.effective_to.is_(None)))}  # fmt: skip
    out = []
    for p in db.scalars(select(Programme).where(Programme.status == "active").order_by(Programme.title)):
        for i in db.scalars(select(Intake).where(Intake.status == "open").order_by(Intake.start_date)):
            f = current.get((p.id, i.id))
            out.append({"programme_id": p.id, "programme": p.title, "intake_id": i.id, "intake": i.label,
                        "tuition_fee": str(f.tuition_fee) if f else None,
                        "minimum_deposit": str(f.minimum_deposit) if f else None,
                        "currency": f.currency if f else "GBP",
                        "effective_from": f.effective_from if f else None})  # fmt: skip
    return out


def set_fee(db: Session, actor: User, programme_id: int, intake_id: int, tuition_fee: Decimal,
            minimum_deposit: Decimal, ip: str | None) -> dict:  # fmt: skip
    """A new schedule closes the current one rather than overwriting it: applications already priced
    keep the schedule they were priced against (application_fees points at the old row)."""
    _require(actor, FEE_ROLES)
    programme, intake = db.get(Programme, programme_id), db.get(Intake, intake_id)
    if programme is None or intake is None:
        raise NotFound()
    if programme.status != "active" or intake.status != "open":
        raise Conflict("ERR_INVALID_STATE", "Fees can only be set for an active programme and an open intake.")
    if tuition_fee <= 0:
        raise _bad("tuition_fee", "Enter a tuition fee above zero.")
    if minimum_deposit < 0 or minimum_deposit > tuition_fee:
        raise _bad("minimum_deposit", "The deposit must be between zero and the tuition fee.")
    today = date.today()
    current = db.scalars(select(FeeSchedule).where(
        FeeSchedule.programme_id == programme_id, FeeSchedule.intake_id == intake_id,
        FeeSchedule.effective_to.is_(None)).with_for_update()).first()  # fmt: skip
    if current is not None:
        if current.tuition_fee == tuition_fee and current.minimum_deposit == minimum_deposit:
            raise Conflict("ERR_UNCHANGED", "Those are already the current fees.")
        current.effective_to = today
        db.flush()
    new = FeeSchedule(programme_id=programme_id, intake_id=intake_id, tuition_fee=tuition_fee,
                      minimum_deposit=minimum_deposit, effective_from=today, created_by=actor.id)  # fmt: skip
    db.add(new)
    db.flush()
    was = f"{current.tuition_fee} (deposit {current.minimum_deposit})" if current else "none"
    audit.write(db, actor, "finance.schedule_change", entity_type="fee_schedule", entity_id=new.id,
                detail=f"Fees for programme {programme_id}, intake {intake_id}: {was} -> {tuition_fee} "
                       f"(deposit {minimum_deposit}), effective {today}", ip=ip)  # fmt: skip
    return next(f for f in list_fees(db, actor)
                if f["programme_id"] == programme_id and f["intake_id"] == intake_id)  # fmt: skip
