"""Operational commands.

    python -m app.cli create-admin --email it@ese.example --name "Jane Smith"
    python -m app.cli seed-reference            # placeholder programmes/intakes pending ESE input D-05
    python -m app.cli seed-demo                 # demo staff accounts for the test environment only

The first administrator is created here because no one exists yet to create them through the API.
The command prints a one-time activation link rather than a password.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.config import get_settings
from app.database import get_sessionmaker
from app.models.agent import Agent
from app.models.application import Application
from app.models.config import Intake, Programme
from app.models.enums import UserRole
from app.models.user import PasswordResetToken, User
from app.services import audit
from app.schemas.common import normalise_email
from app.services.auth import hash_password, new_link_token, unusable_password_hash

# Placeholders until ESE supplies the catalogue (PDP dependency D-05). Titles are marked as such so
# they cannot be mistaken for ESE's real programmes in a demonstration.
PLACEHOLDER_PROGRAMMES = [
    ("Undergraduate programme A (placeholder — awaiting ESE catalogue)", "bachelors", 36, False),
    ("Undergraduate programme B (placeholder — awaiting ESE catalogue)", "bachelors", 36, False),
    ("Postgraduate programme A (placeholder — awaiting ESE catalogue)", "masters", 12, False),
]
PLACEHOLDER_INTAKES = [
    ("January 2027", date(2027, 1, 18), date(2026, 12, 1)),
    ("September 2027", date(2027, 9, 20), date(2027, 7, 31)),
]

# Demo accounts live on a reserved domain so a stray email can never leave the building (RFC 2606).
# It is .example and not .test: both are reserved, but .test is a special-use name that the email
# validator behind every API field rejects, so a demo applicant on .test could sign in and then be
# refused by the application form for having an invalid email address — which is exactly what
# happened. Keep this domain acceptable to app.schemas.common.normalise_email.
DEMO_DOMAIN = "demo.ese.example"
LEGACY_DEMO_DOMAIN = "demo.ese.test"


def _retire_legacy_demo_domain(db) -> None:  # noqa: ANN001 — Session, imported lazily elsewhere
    """Move any demo account left on the old, invalid domain across to the new one.

    Existing development and training databases already hold the .test accounts, and leaving them
    there would mean two sets of demo users and the broken one still listed in the guide. Renaming
    keeps each account's id, so its audit trail, assessments and applications follow it.

    An application keeps its own copy of the address, snapshotted into `data` when the draft was
    created, and the form shows that copy read-only. Renaming the account alone left the old
    address on screen and made the draft unsubmittable — the applicant cannot edit the field, and
    submission refuses a mismatch. So the stored copy moves with the account.
    """
    stale = list(db.scalars(select(User).where(User.email.like(f"%@{LEGACY_DEMO_DOMAIN}"))))
    renamed = 0
    for user in stale:
        local = user.email.rsplit("@", 1)[0]
        replacement = f"{local}@{DEMO_DOMAIN}"
        if db.scalars(select(User).where(User.email == replacement)).first():
            continue  # already migrated by hand; leave the stale row for an operator to remove
        user.email = replacement
        for app in db.scalars(select(Application).where(Application.applicant_id == user.id)):
            if (app.data or {}).get("email"):
                data = dict(app.data)
                data["email"] = replacement
                app.data = data  # reassign: SQLAlchemy does not track in-place JSON edits
        renamed += 1
    if renamed:
        db.commit()
        print(f"Moved {renamed} demo account(s) from @{LEGACY_DEMO_DOMAIN} to @{DEMO_DOMAIN}, "
              "including the address stored on their applications.")  # fmt: skip


def create_admin(email: str, name: str) -> None:
    # Same validation the API applies to a typed address, so the bootstrap account cannot be one
    # the system would later refuse to accept.
    try:
        email = normalise_email(email) or email
    except Exception:  # noqa: BLE001 — the validator raises the API's own error type
        sys.exit(f"{email} is not a valid email address.")
    db = get_sessionmaker()()
    if db.scalars(select(User).where(User.email == email.lower())).first():
        sys.exit(f"An account already exists for {email}")
    user = User(email=email.lower(), full_name=name, role=UserRole.ADMIN, password_hash=unusable_password_hash())
    db.add(user)
    db.flush()
    plaintext, digest = new_link_token()
    db.add(PasswordResetToken(user_id=user.id, token_hash=digest,
                              expires_at=datetime.now(UTC) + timedelta(hours=24)))  # fmt: skip
    audit.write(db, None, "admin.user_create", entity_type="user", entity_id=user.id,
                detail=f"Bootstrap administrator {email} created from the command line")  # fmt: skip
    db.commit()
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    print("Administrator created. Activation link (valid 24 hours, single use):")
    print(f"{base}/reset-password?token={plaintext}&activate=1")


def seed_reference() -> None:
    db = get_sessionmaker()()
    if db.scalars(select(Programme)).first() is None:
        for title, level, months, atas in PLACEHOLDER_PROGRAMMES:
            db.add(Programme(title=title, qualification_level=level, duration_months=months, atas_required=atas))
    if db.scalars(select(Intake)).first() is None:
        for label, start, deadline in PLACEHOLDER_INTAKES:
            db.add(Intake(label=label, start_date=start, application_deadline=deadline))
    db.commit()
    print("Reference data present.")


def seed_allocation_and_fees() -> None:
    """A current CAS allocation period and a fee schedule per programme/intake.

    The figures are placeholders until ESE confirms them (PDP dependency D-05/D-07). Without a fee
    schedule the deposit gate fails closed, so the demonstration environment needs one.
    """
    from decimal import Decimal

    from sqlalchemy import select as sa_select

    from app.models.cas import CasAllocation
    from app.models.finance import FeeSchedule
    from app.services.settings import get_setting

    db = get_sessionmaker()()
    today = date.today()
    start = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
    end = date(start.year + 1, 8, 31)
    label = f"{start.year}/{str(end.year)[2:]}"
    if not db.scalars(sa_select(CasAllocation).where(CasAllocation.period_label == label)).first():
        db.add(CasAllocation(period_label=label, period_start=start, period_end=end,
                             allocated_count=int(get_setting(db, "CAS_ALLOCATION", 7))))  # fmt: skip
    programmes = list(db.scalars(sa_select(Programme).where(Programme.status == "active")))
    intakes = list(db.scalars(sa_select(Intake).where(Intake.status == "open")))
    for programme in programmes:
        for intake in intakes:
            exists = db.scalars(
                sa_select(FeeSchedule).where(
                    FeeSchedule.programme_id == programme.id,
                    FeeSchedule.intake_id == intake.id,
                    FeeSchedule.effective_to.is_(None),
                )
            ).first()
            if exists:
                continue
            db.add(FeeSchedule(programme_id=programme.id, intake_id=intake.id,
                               tuition_fee=Decimal("15000.00"), minimum_deposit=Decimal("3000.00"),
                               effective_from=today))  # fmt: skip
    db.commit()
    print(f"CAS allocation {label} and placeholder fee schedules present "
          f"({len(programmes)} programme(s) x {len(intakes)} intake(s); tuition 15000, deposit 3000 — placeholders).")


def seed_demo() -> None:
    """Demo staff for the test/training environment (NFR-13). Refused in production."""
    s = get_settings()
    if s.is_production:
        sys.exit("seed-demo is not permitted in production.")
    db = get_sessionmaker()()
    password = "Demo-Password-2026!"  # noqa: S105 — training environment only
    _retire_legacy_demo_domain(db)
    accounts = [(f"{role.value}@{DEMO_DOMAIN}", f"Demo {role.value.title()}", role)
                for role in (UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.FINANCE,
                             UserRole.SENIOR, UserRole.ADMIN)]  # fmt: skip
    # Second reviewers: segregation of duties (FR-55) and the second-line review (FR-52) both need
    # a colleague in the same role, so a single account per role cannot demonstrate the controls.
    accounts += [(f"admissions2@{DEMO_DOMAIN}", "Demo Admissions (second)", UserRole.ADMISSIONS),
                 (f"compliance2@{DEMO_DOMAIN}", "Demo Compliance (second)", UserRole.COMPLIANCE)]  # fmt: skip
    for email, name, role in accounts:
        # Seeding goes straight to the ORM, so nothing here would otherwise apply the validation the
        # API applies to every address a person types. That gap shipped an applicant account whose
        # own address the application form then rejected; this is the guard against a repeat.
        normalise_email(email)
        if db.scalars(select(User).where(User.email == email)).first():
            continue
        db.add(User(email=email, full_name=name, role=role,
                    password_hash=hash_password(password), email_verified_at=datetime.now(UTC)))  # fmt: skip
    db.commit()
    print(f"Demo staff accounts ({len(accounts)}): <role>@{DEMO_DOMAIN} / {password}")
    print("  admissions, admissions2, compliance, compliance2, finance, senior, admin")

    # Recruitment agents (FR-102). Illustrative names, matching the approved prototype's screen 11
    # so the register demonstrates against something recognisable.
    agents = [
        ("ABC Education", "IN", "active", date(2026, 1, 12), date(2027, 1, 12), Decimal("10.0")),
        ("Global Study Link", "CN", "active", date(2026, 3, 2), date(2027, 3, 2), Decimal("10.0")),
        ("FutureEdge", "BR", "active", date(2025, 11, 20), date(2026, 11, 20), Decimal("10.0")),
        ("EduWorld", "NG", "under_review", date(2025, 6, 4), date(2026, 6, 4), Decimal("8.0")),
        ("Study International", "VN", "active", date(2026, 2, 17), date(2027, 2, 17), Decimal("10.0")),
    ]
    added = 0
    for name, country, status, dd, review, threshold in agents:
        if db.scalars(select(Agent).where(Agent.name == name)).first():
            continue
        db.add(Agent(name=name, country=country, status=status, due_diligence_date=dd,
                     next_review_date=review, refusal_rate_threshold_pct=threshold))  # fmt: skip
        added += 1
    db.commit()
    print(f"Recruitment agents on the register: {len(agents)} ({added} added now).")

    # A demo applicant with a draft, so the applicant portal can be demonstrated and swept for
    # accessibility without first filling in the form by hand.
    applicant_email = f"applicant@{DEMO_DOMAIN}"
    applicant = db.scalars(select(User).where(User.email == applicant_email)).first()
    if applicant is None:
        applicant = User(email=applicant_email, full_name="Demo Applicant", role=UserRole.APPLICANT,
                         password_hash=hash_password(password),
                         email_verified_at=datetime.now(UTC))  # fmt: skip
        db.add(applicant)
        db.commit()
    if not db.scalars(select(Application).where(Application.applicant_id == applicant.id)).first():
        from app.services import applications as app_svc

        app_svc.create_or_get(db, applicant, None)
        db.commit()
    print(f"Demo applicant with a draft application: {applicant_email} / {password}")


def run_job_now(job_name: str) -> None:
    """Run one scheduled job immediately, for testing and for operations.

    The jobs run on a schedule (LLD 13) that a tester should not have to wait for, and an operator
    occasionally needs to force one after fixing whatever made it fail. It goes through the same
    run_job() the scheduler uses, so the attempt is recorded in job_runs either way — a job run by
    hand is not invisible.
    """
    from app.workers.jobs.compliance import JOBS, run_job

    if job_name not in JOBS:
        sys.exit(f"Unknown job '{job_name}'. One of: {', '.join(sorted(JOBS))}")
    db = get_sessionmaker()()
    try:
        run = run_job(db, job_name, JOBS[job_name])
        print(f"{job_name}: {run.outcome}, {run.records_affected} record(s) affected")
        if run.error_detail:
            print(f"  {run.error_detail}")
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    sub.add_parser("seed-reference")
    sub.add_parser("seed-allocation-and-fees")
    sub.add_parser("seed-demo")
    j = sub.add_parser("run-job", help="Run one scheduled job now")
    j.add_argument("name")
    args = parser.parse_args(argv)
    if args.cmd == "create-admin":
        create_admin(args.email, args.name)
    elif args.cmd == "seed-reference":
        seed_reference()
        seed_allocation_and_fees()
    elif args.cmd == "seed-allocation-and-fees":
        seed_allocation_and_fees()
    elif args.cmd == "seed-demo":
        seed_demo()
    elif args.cmd == "run-job":
        run_job_now(args.name)


if __name__ == "__main__":
    main()
