"""The scheduled compliance jobs (LLD 13).

Idempotency is the property under test throughout. A job may run twice in a day after a restart,
and the second run must be a no-op — not a second reportable event (T-13) and not a second alert
to the same inbox. Each run must also leave a job_runs row, including when it fails.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.operations import JOB_FAILED, JOB_SUCCESS
from app.workers.jobs.compliance import (
    deadline_sweep,
    engagement_check,
    expiry_alerts,
    non_enrolment_detection,
    notification_dispatch,
    recent_failures,
    run_job,
)
from tests.fixtures import API, assign_cas, attend, enrol, run_to_precas, status_change, student_for


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def session(owner_engine: Engine) -> Session:
    from app.database import get_sessionmaker

    db = get_sessionmaker()()
    yield db
    db.close()


@pytest.fixture
def student(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    record = student_for(client, team["compliance"], draft["id"])
    record["application_id"] = draft["id"]
    return record


def _backdate_course_start(owner_engine: Engine, days: int) -> None:
    """Move the course start into the past so the non-enrolment window has elapsed."""
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE cas SET start_date = current_date - :d"), {"d": days})
        conn.commit()


# --- T-13: non-enrolment detection is idempotent -------------------------------------------------
def test_t13_non_enrolment_runs_twice_creates_one_event(client, student, session, owner_engine):
    _backdate_course_start(owner_engine, 30)

    first = non_enrolment_detection(session)
    session.commit()
    second = non_enrolment_detection(session)
    session.commit()

    assert first == 1
    assert second == 0
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event_type, source, status FROM reportable_events")
        ).all()
        status = conn.execute(text("SELECT enrolment_status FROM student_records")).scalar_one()
    assert rows == [("non_enrolment", "automatic", "pending")]
    assert status == "non_enrolled"


def test_non_enrolment_leaves_an_enrolled_student_alone(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"])
    _backdate_course_start(owner_engine, 30)
    assert non_enrolment_detection(session) == 0


def test_non_enrolment_waits_for_the_grace_period(client, student, session, owner_engine):
    _backdate_course_start(owner_engine, 2)  # grace is 14 days
    assert non_enrolment_detection(session) == 0


def test_non_enrolment_event_carries_a_working_day_deadline(client, student, session, owner_engine):
    _backdate_course_start(owner_engine, 30)
    non_enrolment_detection(session)
    session.commit()
    with owner_engine.connect() as conn:
        event_date, deadline = conn.execute(
            text("SELECT event_date, deadline_date FROM reportable_events")
        ).one()
    assert deadline > event_date
    assert deadline.weekday() < 5  # never lands on a weekend


# --- deadline_sweep: warn, then escalate, each once ------------------------------------------------
def test_deadline_sweep_escalates_a_breached_event_once(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"])
    with owner_engine.connect() as conn:
        # Both dates move: the schema will not allow a deadline before the event it belongs to.
        conn.execute(text(
            "UPDATE reportable_events SET event_date = current_date - 20, deadline_date = current_date - 1"))
        conn.commit()

    first = deadline_sweep(session)
    session.commit()
    second = deadline_sweep(session)
    session.commit()
    assert first == 1
    assert second == 0

    with owner_engine.connect() as conn:
        escalated = conn.execute(text("SELECT escalated_at FROM reportable_events")).scalar_one()
        alerts = conn.execute(
            text("SELECT count(*) FROM notification_queue WHERE template_key = 'event.escalated'")
        ).scalar_one()
        audited = conn.execute(
            text("SELECT count(*) FROM audit_entries WHERE action = 'event.escalated'")
        ).scalar_one()
    assert escalated is not None
    assert alerts == 1
    assert audited == 1


def test_deadline_sweep_warns_before_the_deadline_once(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"])
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE reportable_events SET deadline_date = current_date + 2"))
        conn.commit()

    assert deadline_sweep(session) == 1
    session.commit()
    assert deadline_sweep(session) == 0
    session.commit()
    with owner_engine.connect() as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM notification_queue WHERE template_key = 'event.deadline_approaching'")
        ).scalar_one()
    assert queued == 1


def test_a_reported_event_is_not_swept(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"])
    event = status_change(client, team["compliance"], student["id"]).json()["events"][0]
    client.post(f"{API}/events/{event['id']}/report", headers=team["compliance"].headers,
                json={"sms_reference": "SMS-1", "reported_date": date.today().isoformat()})  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text(
            "UPDATE reportable_events SET event_date = current_date - 20, deadline_date = current_date - 5"))
        conn.commit()
    assert deadline_sweep(session) == 0


# --- engagement_check ------------------------------------------------------------------------------
def test_engagement_check_alerts_once_below_the_threshold(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"])
    for i, status in enumerate(["absent", "absent", "present"]):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"M{i}", status=status)  # fmt: skip

    assert engagement_check(session) == 1
    session.commit()
    assert engagement_check(session) == 0


def test_engagement_check_ignores_a_student_above_the_threshold(client, student, team, session):
    enrol(client, team["compliance"], student["id"])
    for i in range(5):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"M{i}", status="present")  # fmt: skip
    assert engagement_check(session) == 0


# --- expiry_alerts ------------------------------------------------------------------------------------
def test_expiry_alerts_fire_once_per_expiring_permission(client, student, team, session, owner_engine):
    enrol(client, team["compliance"], student["id"],
          permission_expiry=(date.today() + timedelta(days=30)).isoformat())  # fmt: skip
    assert expiry_alerts(session) >= 1
    session.commit()
    assert expiry_alerts(session) == 0


# --- job_runs and dispatch ------------------------------------------------------------------------------
def test_every_run_is_recorded_including_failures(session, owner_engine):
    ok = run_job(session, "test_job", lambda db: 3)
    assert ok.outcome == JOB_SUCCESS and ok.records_affected == 3 and ok.finished_at is not None

    def explode(db):  # noqa: ANN001, ANN202
        raise RuntimeError("upstream unavailable")

    bad = run_job(session, "test_job", explode)
    assert bad.outcome == JOB_FAILED
    assert "upstream unavailable" in bad.error_detail
    assert recent_failures(session, "test_job") == 1

    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT outcome, records_affected FROM job_runs WHERE job_name = 'test_job' ORDER BY id")
        ).all()
    assert rows == [("success", 3), ("failed", 0)]


def test_notification_dispatch_sends_what_the_jobs_queued(client, student, team, session, owner_engine):
    from app.services import notifications

    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"])
    with owner_engine.connect() as conn:
        # Both dates move: the schema will not allow a deadline before the event it belongs to.
        conn.execute(text(
            "UPDATE reportable_events SET event_date = current_date - 20, deadline_date = current_date - 1"))
        conn.commit()
    deadline_sweep(session)
    session.commit()

    notifications.OUTBOX.clear()
    sent = notification_dispatch(session)
    session.commit()
    assert sent == 1
    assert notifications.OUTBOX[0].template_key == "event.escalated"
    assert notification_dispatch(session) == 0  # nothing left queued


# --- A job must not lose one student's work because another was a repeat --------------------------
def test_a_repeat_alert_does_not_discard_the_rest_of_the_run(
    client, applicant, draft, reference, team, staff, session, owner_engine
):
    """Regression: a duplicate alert used to roll back the caller's whole transaction, so an
    escalation raised earlier in the same sweep silently vanished and was never raised again."""
    from tests.fixtures import register

    # Student one: already alerted on a previous, committed run.
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)
    first = student_for(client, team["compliance"], draft["id"])
    enrol(client, team["compliance"], first["id"])
    status_change(client, team["compliance"], first["id"], change_type="course_change",
                  reason="Moved to the evening cohort at the student's request.")  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE reportable_events SET deadline_date = current_date + 2"))
        conn.commit()
    assert deadline_sweep(session) == 1  # the approaching-deadline alert is queued
    session.commit()

    # Student two: a fresh breach that must be escalated on the next sweep.
    second_applicant = register(client, "second.applicant@example.com", "Arun Nair")
    second_draft = client.post(f"{API}/applications", headers=second_applicant.headers).json()
    run_to_precas(client, second_applicant, second_draft["id"], reference, team["admissions"],
                  team["compliance"], team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], second_draft["id"], reference, cas_number="E4G0ZZZ98765")
    second = student_for(client, team["compliance"], second_draft["id"])
    enrol(client, team["compliance"], second["id"])
    status_change(client, team["compliance"], second["id"], change_type="interruption",
                  reason="Student interrupted study for three months on medical advice.")  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text(
            "UPDATE reportable_events SET event_date = current_date - 20, deadline_date = current_date - 1 "
            "WHERE student_id = :s"), {"s": second["id"]})  # fmt: skip
        conn.commit()

    # This sweep meets student one's duplicate alert and student two's new breach in one loop.
    session.expire_all()
    assert deadline_sweep(session) == 1
    session.commit()

    with owner_engine.connect() as conn:
        escalated = conn.execute(
            text("SELECT escalated_at FROM reportable_events WHERE student_id = :s"), {"s": second["id"]}
        ).scalar_one()
        alerts = conn.execute(
            text("SELECT count(*) FROM notification_queue WHERE template_key = 'event.escalated'")
        ).scalar_one()
    assert escalated is not None, "the escalation was discarded by the duplicate alert"
    assert alerts == 1


def test_export_cleanup_deletes_an_expired_pack(client, student, team, session, owner_engine):
    """LLD 13.1: a personal-data export must not linger in object storage after 24 hours."""
    from app.services.storage import get_storage
    from app.workers.jobs.compliance import export_cleanup

    job = client.post(f"{API}/applications/{student['application_id']}/evidence-pack",
                      headers=team["senior"].headers).json()  # fmt: skip
    with owner_engine.connect() as conn:
        key = conn.execute(text("SELECT result_storage_key FROM export_jobs")).scalar_one()
        conn.execute(text("UPDATE export_jobs SET expires_at = now() - interval '1 hour'"))
        conn.commit()
    assert key in getattr(get_storage(), "objects", {key: None})

    session.expire_all()
    assert export_cleanup(session) == 1
    session.commit()
    assert key not in getattr(get_storage(), "objects", {})

    gone = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    assert gone.status_code in (404, 409)


def test_calendar_headroom_warns_before_the_calendar_runs_out(session, owner_engine):
    """The system refuses to guess a deadline; this is the warning that arrives before it has to."""
    from datetime import date as _date

    from app.workers.jobs.compliance import calendar_headroom_check

    with owner_engine.connect() as conn:
        latest = conn.execute(text("SELECT max(holiday_date) FROM bank_holidays")).scalar_one()
        conn.commit()
    session.expire_all()
    assert calendar_headroom_check(session, today=_date(latest.year - 3, 1, 1)) == 0  # plenty of room
    session.rollback()
    assert calendar_headroom_check(session, today=latest - timedelta(days=30)) == 1
    session.commit()
    with owner_engine.connect() as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM notification_queue WHERE template_key = 'calendar.expiring'")
        ).scalar_one()
    assert queued == 1
