"""Scheduled worker process (LLD 13) — runs separately from the API.

The compliance jobs run here on the LLD 13 schedule. Each writes a job_runs row whatever happens to
it, because an event that was never reported and a job that silently stopped running look identical
from outside unless the runs are recorded.

Registered: deadline_sweep, non_enrolment_detection, engagement_check, expiry_alerts,
export_cleanup, calendar_headroom_check, notification_dispatch and agent_review_reminder. Still to
come: metric_refresh, retention_sweep.

    python -m app.workers.scheduler
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from app.database import get_sessionmaker
from app.services import cache
from app.workers.jobs.compliance import JOBS, run_job

log = logging.getLogger("ese.worker")
HEARTBEAT_KEY = "worker:heartbeat"


def heartbeat() -> None:
    cache.get_redis().set(HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=300)


def run_named(job_name: str) -> None:
    """Run one job in its own session, recording the attempt in job_runs (LLD 13)."""
    session = get_sessionmaker()()
    try:
        run = run_job(session, job_name, JOBS[job_name])
        log.info("job=%s outcome=%s affected=%s", job_name, run.outcome, run.records_affected)
    finally:
        session.close()


# LLD 13. Times are Europe/London so that "daily 02:00" means what an operator reads it to mean.
SCHEDULE: dict[str, dict] = {
    "deadline_sweep": {"trigger": "cron", "hour": "7-19", "minute": 5},
    "non_enrolment_detection": {"trigger": "cron", "hour": 2, "minute": 0},
    "engagement_check": {"trigger": "cron", "hour": 3, "minute": 0},
    "expiry_alerts": {"trigger": "cron", "hour": 6, "minute": 0},
    "export_cleanup": {"trigger": "cron", "hour": 1, "minute": 30},
    "calendar_headroom_check": {"trigger": "cron", "day_of_week": "mon", "hour": 8, "minute": 15},
    "notification_dispatch": {"trigger": "interval", "minutes": 5},
    "agent_review_reminder": {"trigger": "cron", "hour": 6, "minute": 20},
}


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Europe/London")
    scheduler.add_job(heartbeat, "interval", seconds=60, id="heartbeat", next_run_time=datetime.now(UTC))
    for job_name, trigger in SCHEDULE.items():
        options = dict(trigger)
        scheduler.add_job(
            run_named, args=[job_name], id=job_name, max_instances=1, coalesce=True,
            misfire_grace_time=3600, **options,
        )  # fmt: skip
    return scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Worker starting")
    while not cache.ping():
        log.warning("Waiting for Redis…")
        time.sleep(2)
    build_scheduler().start()


if __name__ == "__main__":
    main()
