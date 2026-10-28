"""The compliance jobs (LLD 13).

Each job is a plain function taking a Session, so it can be run by the scheduler, by an operator
from the command line, or by a test — without any of them being a special case. Every run writes a
job_runs row, because "the event was never reported" and "the job that would have raised it stopped
running three weeks ago" look identical from the outside unless the runs are recorded.

Idempotency is a property of each job, not of the scheduler: a job may run twice in a day after a
restart, and must not raise a duplicate event or a duplicate alert when it does (T-13).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import Document
from app.models.cas import CAS_ASSIGNED, Cas
from app.models.compliance_events import EVENT_PENDING, ReportableEvent
from app.models.enums import EnrolmentStatus, EventType
from app.models.offer import Offer
from app.models.operations import JOB_FAILED, JOB_SUCCESS, JobRun
from app.models.student import StudentRecord
from app.services import agents, events, notifications, students
from app.services.settings import get_setting

log = logging.getLogger("ese.worker.jobs")


def run_job(db: Session, name: str, fn: Callable[[Session], int]) -> JobRun:
    """Run a job, recording the attempt whatever happens to it (LLD 13)."""
    run = JobRun(job_name=name, started_at=datetime.now(UTC))
    db.add(run)
    db.commit()
    try:
        affected = fn(db)
        db.commit()
        run.records_affected = affected
        run.outcome = JOB_SUCCESS
    except Exception as exc:  # noqa: BLE001 — a job must never take the worker down with it
        db.rollback()
        run.outcome = JOB_FAILED
        run.error_detail = f"{type(exc).__name__}: {exc}"[:2000]
        log.exception("Job %s failed", name)
    finally:
        run.finished_at = datetime.now(UTC)
        db.add(run)
        db.commit()
    return run


def recent_failures(db: Session, job_name: str, window: int = 3) -> int:
    """Consecutive failures at the end of the run history (LLD 13: three raises an alert)."""
    rows = db.scalars(
        select(JobRun.outcome)
        .where(JobRun.job_name == job_name, JobRun.outcome.isnot(None))
        .order_by(JobRun.started_at.desc())
        .limit(window)
    ).all()
    streak = 0
    for outcome in rows:
        if outcome != JOB_FAILED:
            break
        streak += 1
    return streak


# --- non_enrolment_detection (FR-73, T-13) ------------------------------------------------------
def non_enrolment_detection(db: Session, today: date | None = None) -> int:
    """Students who never turned up: course start plus the grace period, still awaiting arrival.

    Runs daily. The partial unique index and the check in events.create_event make a second run on
    the same day a no-op rather than a duplicate event (T-13).
    """
    today = today or date.today()
    grace = int(get_setting(db, "NON_ENROLMENT_GRACE_DAYS", 14))
    cutoff = today - timedelta(days=grace)
    candidates = db.scalars(
        select(StudentRecord)
        .join(Cas, Cas.id == StudentRecord.cas_id)
        .where(
            StudentRecord.enrolment_status == EnrolmentStatus.AWAITING_ARRIVAL,
            Cas.status == CAS_ASSIGNED,
            Cas.start_date <= cutoff,
        )
    ).all()

    created = 0
    for student in candidates:
        cas = db.get(Cas, student.cas_id)
        event_date = cas.start_date + timedelta(days=grace)
        event = events.create_event(
            db, student, None,
            event_type=EventType.NON_ENROLMENT,
            event_date=event_date,
            source=events.SOURCE_AUTOMATIC,
            detail=(f"No enrolment recorded by {event_date.isoformat()} "
                    f"({grace} days after the course start of {cas.start_date.isoformat()})"),
        )  # fmt: skip
        if event is None:
            continue  # already raised on an earlier run
        students.close_record(
            db, student, None,
            reason_code="non_enrolment",
            detail=(f"Record closed as non-enrolled: no enrolment by {event_date.isoformat()}. "
                    "Reportable event raised automatically."),
        )  # fmt: skip
        notifications.enqueue(
            db, "event.created", notifications.compliance_inbox(db),
            {"event": "non-enrolment", "student_id": student.id,
             "deadline": event.deadline_date.isoformat()},  # fmt: skip
            dedupe_key=f"created:{event.id}", student_id=student.id,
            application_id=student.application_id,
        )
        created += 1
    return created


# --- deadline_sweep (FR-97, FR-100) -------------------------------------------------------------
def deadline_sweep(db: Session, today: date | None = None) -> int:
    """Warn on approaching deadlines and escalate breached ones (FR-97, FR-100).

    Both halves are idempotent: the approaching notice is deduplicated by its key, and an
    escalation stamps escalated_at once, so the hourly schedule cannot raise it twice.
    """
    today = today or date.today()
    warn_days = int(get_setting(db, "ESCALATION_DAYS_BEFORE", 3))
    # Deadline order, so the sweep acts on the most urgent first and its behaviour does not depend
    # on the order the database happens to return rows in.
    pending = db.scalars(
        select(ReportableEvent)
        .where(ReportableEvent.status == EVENT_PENDING)
        .order_by(ReportableEvent.deadline_date, ReportableEvent.id)
    ).all()
    touched = 0
    for event in pending:
        if event.deadline_date < today:
            if events.escalate(db, event):
                notifications.enqueue(
                    db, "event.escalated", notifications.authorising_officer(db),
                    {"event": event.event_type.value, "deadline": event.deadline_date.isoformat(),
                     "student_id": event.student_id},  # fmt: skip
                    dedupe_key=f"escalated:{event.id}", student_id=event.student_id,
                )
                touched += 1
        elif (event.deadline_date - today).days <= warn_days:
            sent = notifications.enqueue(
                db, "event.deadline_approaching", notifications.compliance_inbox(db),
                {"event": event.event_type.value, "deadline": event.deadline_date.isoformat(),
                 "student_id": event.student_id},  # fmt: skip
                dedupe_key=f"approaching:{event.id}:{event.deadline_date.isoformat()}",
                student_id=event.student_id,
            )
            if sent:
                touched += 1
    return touched


# --- engagement_check (FR-78) --------------------------------------------------------------------
def engagement_check(db: Session, today: date | None = None) -> int:
    """Attendance below the configured threshold raises an alert, once per breach.

    Two failures are possible here and they pull in opposite directions. Keying the alert on the
    lookback window — which slides by a day every day — made a single unresolved breach email the
    compliance inbox every morning indefinitely, which is how an inbox stops being read. Keying it
    on time alone instead would silence a *genuinely new* breach from a student who had recovered
    in between, which is a missed engagement breach on a sponsored student and the worse of the
    two. So the hold-off belongs to the breach: it is stamped on the student record when the
    alert goes out and cleared the moment they are back above the threshold. While one breach
    persists the alert repeats no more often than ENGAGEMENT_ALERT_REPEAT_DAYS, because an
    unresolved breach should not be forgotten either.
    """
    today = today or date.today()
    window_start = today - timedelta(days=30)
    repeat_days = int(get_setting(db, "ENGAGEMENT_ALERT_REPEAT_DAYS", 14))
    quiet_until = datetime.combine(today - timedelta(days=repeat_days), time.min, tzinfo=UTC)
    enrolled = db.scalars(
        select(StudentRecord).where(StudentRecord.enrolment_status == EnrolmentStatus.ENROLLED)
    ).all()
    alerted = 0
    for student in enrolled:
        summary = students.attendance_summary(db, student, since=window_start)
        if not summary.has_data or not summary.below_threshold:
            # Recovered, or nothing to judge yet: the next breach is a new one.
            if student.engagement_alerted_at is not None:
                student.engagement_alerted_at = None
                db.flush()
            continue
        if student.engagement_alerted_at is not None and student.engagement_alerted_at > quiet_until:
            continue
        if notifications.enqueue(
            db, "attendance.threshold_breached", notifications.compliance_inbox(db),
            {"student_id": student.id, "percentage": summary.percentage, "threshold": summary.threshold},
            dedupe_key=f"engagement:{student.id}:{today.isoformat()}", student_id=student.id,
        ):
            student.engagement_alerted_at = datetime.combine(today, time.min, tzinfo=UTC)
            db.flush()
            alerted += 1
    return alerted


# --- expiry_alerts (FR-23, FR-42, FR-81) ----------------------------------------------------------
def expiry_alerts(db: Session, today: date | None = None) -> int:
    """Permission, document and offer expiries, each with its own configured notice period."""
    today = today or date.today()
    permission_days = int(get_setting(db, "PERMISSION_EXPIRY_ALERT_DAYS", 60))
    offer_days = int(get_setting(db, "OFFER_EXPIRY_ALERT_DAYS", 7))
    sent = 0

    horizon = today + timedelta(days=permission_days)
    for student in db.scalars(
        select(StudentRecord).where(
            StudentRecord.permission_expiry.isnot(None),
            StudentRecord.permission_expiry <= horizon,
            StudentRecord.permission_expiry >= today,
            StudentRecord.enrolment_status == EnrolmentStatus.ENROLLED,
        )
    ).all():
        if notifications.enqueue(
            db, "permission.expiring", notifications.compliance_inbox(db),
            {"student_id": student.id, "expires": student.permission_expiry.isoformat()},
            dedupe_key=f"permission:{student.id}:{student.permission_expiry.isoformat()}",
            student_id=student.id,
        ):
            sent += 1

    for document in db.scalars(
        select(Document).where(
            Document.expiry_date.isnot(None),
            Document.expiry_date <= horizon,
            Document.expiry_date >= today,
            Document.superseded_by.is_(None),
        )
    ).all():
        if notifications.enqueue(
            db, "document.expiring", notifications.compliance_inbox(db),
            {"document_id": document.id, "category": str(document.category),
             "expires": document.expiry_date.isoformat()},  # fmt: skip
            dedupe_key=f"document:{document.id}:{document.expiry_date.isoformat()}",
            application_id=document.application_id,
        ):
            sent += 1

    offer_horizon = today + timedelta(days=offer_days)
    for offer in db.scalars(
        select(Offer).where(
            Offer.expiry_date.isnot(None),
            Offer.expiry_date <= offer_horizon,
            Offer.expiry_date >= today,
            Offer.response.is_(None),
            Offer.issued_at.isnot(None),
        )
    ).all():
        if notifications.enqueue(
            db, "offer.expiring", notifications.compliance_inbox(db),
            {"offer_id": offer.id, "expires": offer.expiry_date.isoformat()},
            dedupe_key=f"offer:{offer.id}:{offer.expiry_date.isoformat()}",
            application_id=offer.application_id,
        ):
            sent += 1
    return sent


def agent_review_reminder(db: Session, today: date | None = None) -> int:
    """Agent reviews coming due, and overdue ones (BRD FR-91).

    One alert when the review enters the notice window, and one more if the date passes without a
    new review being recorded. Recording the review moves next_review_date, which changes the
    dedupe key, so the next cycle alerts afresh. Suspended and deactivated agents take no
    applications and are not chased.
    """
    today = today or date.today()
    notice_days = int(get_setting(db, "AGENT_REVIEW_ALERT_DAYS", 30))
    sent = 0
    for agent in agents.reviews_due(db, today + timedelta(days=notice_days)):
        overdue = agent.next_review_date < today
        stage = "overdue" if overdue else "due"
        if notifications.enqueue(
            db, "agent.review_due", notifications.compliance_inbox(db),
            {"agent": agent.name, "country": agent.country, "review_date": agent.next_review_date.isoformat(),
             "when": "overdue" if overdue else "due", "link": notifications.link(f"/staff/agents/{agent.id}")},
            dedupe_key=f"agent-review:{agent.id}:{agent.next_review_date.isoformat()}:{stage}",
        ):
            sent += 1
    return sent


def export_cleanup(db: Session, today: date | None = None) -> int:
    """Delete expired export results (LLD 13.1).

    Expiry is enforced at the API, but an expired pack that stays in object storage is still a
    file full of personal data sitting where nobody expects it. This is what makes "deleted 24
    hours after completion" true rather than merely promised.
    """
    from app.models.operations import EXPORT_READY, ExportJob
    from app.services.storage import get_storage

    storage = get_storage()
    removed = 0
    for job in db.scalars(
        select(ExportJob).where(
            ExportJob.status == EXPORT_READY,
            ExportJob.result_storage_key.isnot(None),
            ExportJob.expires_at < datetime.now(UTC),
        )
    ).all():
        try:
            storage.delete(job.result_storage_key)
        except Exception:  # noqa: BLE001 — an already-missing object is the desired end state
            log.warning("Export %s: storage object already gone", job.id)
        job.result_storage_key = None
        job.error_detail = "Expired and deleted 24 hours after it was produced."
        db.flush()
        removed += 1
    return removed


def calendar_headroom_check(db: Session, today: date | None = None) -> int:
    """Warn while there is still time to load the next year of bank holidays (LLD 8.2).

    When the calendar runs out, every status change and every event-raising job fails — correctly,
    but abruptly. This gives compliance months of notice instead.
    """
    from app.services.deadlines import CALENDAR_HEADROOM_WARNING_DAYS, calendar_headroom

    headroom = calendar_headroom(db, today)
    if headroom is None or headroom > CALENDAR_HEADROOM_WARNING_DAYS:
        return 0
    queued = notifications.enqueue(
        db, "calendar.expiring", notifications.compliance_inbox(db),
        {"days": headroom},
        dedupe_key=f"calendar:{(today or date.today()).isoformat()[:7]}",
    )  # fmt: skip
    return 1 if queued else 0


def notification_dispatch(db: Session, today: date | None = None) -> int:
    """Send what the other jobs queued (FR-118)."""
    return notifications.dispatch_queued(db)


JOBS: dict[str, Callable[[Session], int]] = {
    "non_enrolment_detection": non_enrolment_detection,
    "deadline_sweep": deadline_sweep,
    "engagement_check": engagement_check,
    "expiry_alerts": expiry_alerts,
    "export_cleanup": export_cleanup,
    "calendar_headroom_check": calendar_headroom_check,
    "notification_dispatch": notification_dispatch,
    "agent_review_reminder": agent_review_reminder,
}
