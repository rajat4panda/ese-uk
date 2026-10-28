"""Operational tables: scheduled job runs and asynchronous export jobs (LLD 3.13).

job_runs is the evidence that the compliance jobs actually ran. An unreported event because a job
silently stopped is indistinguishable, from the Home Office's side, from an event nobody looked at.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, created_at_col, pk

JOB_SUCCESS = "success"
JOB_FAILED = "failed"

EXPORT_PENDING = "pending"
EXPORT_RUNNING = "running"
EXPORT_READY = "ready"
EXPORT_FAILED = "failed"

NOTIFICATION_QUEUED = "queued"
NOTIFICATION_SENT = "sent"
NOTIFICATION_FAILED = "failed"
MAX_SEND_ATTEMPTS = 3

EXPORT_RESULT_HOURS = 24  # LLD 13.1: a personal-data export must not linger in object storage


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = pk()
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    outcome: Mapped[str | None] = mapped_column(Text)
    records_affected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_detail: Mapped[str | None] = mapped_column(Text)


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str | None] = mapped_column(Text)
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT")
    )
    requested_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=EXPORT_PENDING)
    result_storage_key: Mapped[str | None] = mapped_column(Text)
    result_filename: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NotificationQueueRow(Base):
    """LLD 3.13 notification_queue. dedupe_key is what stops a job that runs twice sending the
    same alert twice — the uniqueness is enforced by the database, not by the caller's memory."""

    __tablename__ = "notification_queue"

    id: Mapped[int] = pk()
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT")
    )
    student_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("student_records.id", ondelete="RESTRICT")
    )
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=NOTIFICATION_QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    send_after: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = created_at_col()
