"""Test configuration.

Tests run against real PostgreSQL 16 and Redis, as the application does:
  * the schema is built by the Alembic migrations (so the migrations themselves are tested),
  * the application connects as ese_app (member of app_role), exactly as in production, so the
    audit grant (T-15) is exercised for real,
  * cleanup between tests uses the schema-owner connection, because app_role cannot delete.

Override connection strings with TEST_DATABASE_URL / TEST_MIGRATION_DATABASE_URL / TEST_REDIS_URL.
"""

from __future__ import annotations

import os

os.environ.update(
    ESE_ENV_FILE="",  # ignore the developer's .env: the suite sets everything it needs here
    ENVIRONMENT="test",
    DATABASE_URL=os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg://ese_app:ese_app_local@localhost:5432/ese_test"
    ),
    MIGRATION_DATABASE_URL=os.environ.get(
        "TEST_MIGRATION_DATABASE_URL", "postgresql+psycopg://ese_owner:ese_owner_local@localhost:5432/ese_test"
    ),
    REDIS_URL=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
    SECRET_KEY="test-secret-key-that-is-at-least-32-characters-long",
    STORAGE_BACKEND="memory",
    MAIL_BACKEND="memory",
    ANTIVIRUS_BACKEND="simulated",
    RATE_LIMIT_ENABLED="false",
    PUBLIC_BASE_URL="https://ese.test",
    SERVE_WEB_CLIENT="false",
)

from collections.abc import Iterator  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.config import get_settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(get_settings().migration_url, future=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["url"] = get_settings().migration_url
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


TABLES = (
    "audit_entries, notification_queue, export_jobs, job_runs, escalations, reportable_events, student_notes, status_checks, student_contact_changes, "
    "attendance_records, student_records, visa_outcomes, cas, cas_allocations, payments, application_fees, "
    "fee_schedules, offer_conditions, offers, academic_assessments, compliance_reviews, documents, "
    "applications, agents, intakes, programmes, password_reset_tokens, email_verification_tokens, "
    "system_settings, users"
)


def load_migration(name: str):
    import importlib.util

    path = os.path.join(ROOT, "migrations", "versions", f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SETTINGS_SEED = load_migration("0002_auth_operational").SETTINGS + load_migration("0008_agent_management").SETTINGS
BANK_HOLIDAYS = load_migration("0004_post_cas_compliance").BANK_HOLIDAYS


@pytest.fixture(autouse=True)
def clean_state(owner_engine: Engine) -> Iterator[None]:
    from app.services import notifications
    from app.services.cache import get_redis
    from app.services.storage import get_storage

    with owner_engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        # bank_holidays is reference data seeded by migration 0004, and a wrong or missing row
        # silently changes every reporting deadline. Restore it between tests rather than trusting
        # each test to put back what it borrowed.
        conn.execute(text("DELETE FROM bank_holidays"))
        for holiday_date, name in BANK_HOLIDAYS:
            conn.execute(text("INSERT INTO bank_holidays (holiday_date, name) VALUES (:d, :n)"),
                         {"d": holiday_date, "n": name})  # fmt: skip
        for key, value, value_type in SETTINGS_SEED:
            conn.execute(text("INSERT INTO system_settings (key, value, value_type) VALUES (:k, :v, :t)"),
                         {"k": key, "v": value, "t": value_type})
    get_redis().flushdb()
    notifications.OUTBOX.clear()
    storage = get_storage()
    if hasattr(storage, "objects"):
        storage.objects.clear()
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def reference(owner_engine: Engine) -> dict:
    """An active programme and an open intake starting ~4 months from today."""
    start = date.today() + timedelta(days=120)
    deadline = date.today() + timedelta(days=60)
    with owner_engine.connect() as conn:
        pid = conn.execute(text(
            "INSERT INTO programmes (title, qualification_level, duration_months) "
            "VALUES ('BSc Test Programme', 'bachelors', 36) RETURNING id")).scalar_one()  # fmt: skip
        iid = conn.execute(text(
            "INSERT INTO intakes (label, start_date, application_deadline) VALUES ('Test intake', :s, :d) RETURNING id"),
            {"s": start, "d": deadline}).scalar_one()  # fmt: skip
        closed = conn.execute(text(
            "INSERT INTO intakes (label, start_date, application_deadline, status) "
            "VALUES ('Closed intake', :s, :d, 'closed') RETURNING id"), {"s": start, "d": deadline}).scalar_one()  # fmt: skip
        agent = conn.execute(text(
            "INSERT INTO agents (name, country) VALUES ('Test Agent Ltd', 'IN') RETURNING id")).scalar_one()  # fmt: skip
        suspended = conn.execute(text(
            "INSERT INTO agents (name, country, status) VALUES ('Suspended Agent', 'NG', 'suspended') RETURNING id"
        )).scalar_one()  # fmt: skip
        fee = conn.execute(text(
            "INSERT INTO fee_schedules (programme_id, intake_id, tuition_fee, minimum_deposit, effective_from) "
            "VALUES (:p, :i, 15000, 3000, current_date) RETURNING id"), {"p": pid, "i": iid}).scalar_one()  # fmt: skip
        allocation = conn.execute(text(
            "INSERT INTO cas_allocations (period_label, period_start, period_end, allocated_count) "
            "VALUES ('Test period', current_date - 30, current_date + 300, 7) RETURNING id")).scalar_one()  # fmt: skip
    return {"programme_id": pid, "intake_id": iid, "closed_intake_id": closed, "intake_start": start,
            "agent_id": agent, "suspended_agent_id": suspended, "fee_schedule_id": fee,
            "allocation_id": allocation}  # fmt: skip


from tests.fixtures import *  # noqa: E402, F403
