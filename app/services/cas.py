"""CAS assignment (LLD 3.6, 7.3; FR-53 to FR-61).

The guard chain lives in services/guards.py; this module holds the effects that write the CAS and
the student record, and the allocation arithmetic the warning uses. Every write happens inside the
executor's transaction, so a CAS can never exist without its audit entry (LLD 7.3 figure 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import Conflict, GuardFailed
from app.models.application import Application
from app.models.cas import CAS_ASSIGNED, Cas, CasAllocation
from app.models.enums import EnrolmentStatus
from app.models.student import StudentRecord
from app.models.user import User

CAS_NUMBER = re.compile(r"^[A-Z0-9]{10,20}$")


@dataclass(frozen=True)
class Allocation:
    period: CasAllocation | None
    assigned: int

    @property
    def allocated(self) -> int:
        return self.period.allocated_count if self.period else 0

    @property
    def remaining(self) -> int:
        return self.allocated - self.assigned


def current_allocation(db: Session, on: date | None = None, *, lock: bool = False) -> Allocation:
    """The open allocation period covering the date, with the number of CAS already assigned (FR-60).

    ``lock=True`` takes a row lock on the period so that two assignments running at the same time
    cannot both read the last remaining place (FR-61).
    """
    on = on or date.today()
    stmt = (
        select(CasAllocation)
        .where(CasAllocation.status == "open", CasAllocation.period_start <= on, CasAllocation.period_end >= on)
        .order_by(CasAllocation.period_start.desc())
    )
    if lock:
        stmt = stmt.with_for_update()
    period = db.scalars(stmt).first()
    if period is None:
        return Allocation(None, 0)
    assigned = db.scalar(
        select(func.count()).select_from(Cas).where(Cas.allocation_period_id == period.id, Cas.status == CAS_ASSIGNED)
    )
    return Allocation(period, int(assigned or 0))


def normalise_cas_number(value: str | None) -> str:
    value = "".join((value or "").split()).upper()
    if not CAS_NUMBER.match(value):
        raise GuardFailed("ERR_CAS_FORMAT", "Enter the CAS number exactly as issued by the SMS (10 to 20 characters).",
                          details=[{"field": "cas_number", "code": "ERR_CAS_FORMAT"}])  # fmt: skip
    return value


def validate_payload(db: Session, app: Application, payload: dict) -> dict:
    """Field-level checks made before the guard chain runs, so a malformed request never reaches it."""
    cas_number = normalise_cas_number(payload.get("cas_number"))
    start, end = payload.get("start_date"), payload.get("end_date")
    problems = []

    def bad(field: str, code: str, message: str) -> None:
        problems.append({"field": field, "code": code, "message": message})

    if not isinstance(start, date) or not isinstance(end, date):
        bad("start_date", "ERR_DATE_RANGE", "Course dates are required.")
    elif end <= start:
        bad("end_date", "ERR_DATE_RANGE", "The end date must be after the start date.")
    elif start > date.today().replace(year=date.today().year + 3):
        bad("start_date", "ERR_DATE_RANGE", "The start date is more than three years ahead.")
    if not (payload.get("course_title") or "").strip():
        bad("course_title", "ERR_REQUIRED", "The course title is required.")
    if not (payload.get("level1_user") or "").strip():
        bad("level1_user", "ERR_REQUIRED", "Record the Level 1 User assigning the CAS.")
    # FR-52: a CAS declaring no tuition fee is a data-entry slip, not a free course.
    if not payload.get("tuition_fee"):
        bad("tuition_fee", "ERR_REQUIRED", "Enter the tuition fee exactly as stated on the CAS.")
    paid, fee = payload.get("amount_paid"), payload.get("tuition_fee")
    if paid is not None and fee is not None and paid > fee:
        bad("amount_paid", "ERR_VALIDATION", "The amount paid cannot be more than the tuition fee.")
    if problems:
        raise GuardFailed("ERR_VALIDATION", "Check the CAS details.", details=problems)
    if db.scalars(select(Cas).where(Cas.cas_number == cas_number)).first():
        raise Conflict("ERR_CAS_DUPLICATE", "That CAS number is already recorded against another application.")
    return {**payload, "cas_number": cas_number}


def create_cas(db: Session, app: Application, actor: User, payload: dict) -> None:
    """Effect: write the CAS record (LLD 7.3)."""
    allocation = current_allocation(db, lock=True)
    if allocation.period is None:
        raise GuardFailed(
            "ERR_ALLOCATION_MISSING",
            "No open CAS allocation period covers today. An administrator must record one (FR-60).",
        )
    cas = Cas(
        application_id=app.id,
        cas_number=payload["cas_number"],
        course_title=payload["course_title"].strip(),
        start_date=payload["start_date"],
        end_date=payload["end_date"],
        tuition_fee=payload["tuition_fee"],
        amount_paid=payload["amount_paid"],
        level1_user=payload["level1_user"].strip(),
        authorised_by=actor.id,
        allocation_period_id=allocation.period.id,
        notes=(payload.get("notes") or "").strip(),
        assigned_at=_now(),
    )
    db.add(cas)
    db.flush()
    payload["_cas"] = cas


def create_student_record(db: Session, app: Application, actor: User, payload: dict) -> None:
    """Effect: the student record exists from CAS assignment, before any visa decision (LLD 7.4)."""
    cas: Cas = payload["_cas"]
    record = StudentRecord(
        application_id=app.id,
        cas_id=cas.id,
        enrolment_status=EnrolmentStatus.AWAITING_ARRIVAL,
        current_address=app.data.get("uk_address") or app.data.get("home_address") or {},
        contact_phone=app.data.get("mobile"),
        contact_email=app.data.get("email"),
    )
    db.add(record)
    db.flush()
    payload["_student_record"] = record


def describe_assignment(db: Session, app: Application, payload: dict) -> str:
    cas: Cas = payload["_cas"]
    allocation = current_allocation(db)
    return (
        f"CAS {cas.cas_number} assigned for {cas.course_title} ({cas.start_date} to {cas.end_date}); "
        f"Level 1 User {cas.level1_user}; allocation {allocation.assigned} of {allocation.allocated} used"
    )


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
