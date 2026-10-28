"""Template rendering and dispatch (LLD 12, 13).

Applicant-facing messages are sent directly after their transaction commits. Operational alerts
raised by the scheduled jobs go through notification_queue instead (enqueue), because those run
repeatedly and must not re-send the same alert: the dedupe key is unique in the database, so a job
that runs twice enqueues once (LLD 13).

Templates are still held in code. Moving them into notification_templates with versioning
(FR-115) and writing the communications log (FR-116) is Sprint 4; the keys used here are the
LLD 12 event keys, so that change alters storage, not callers.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from string import Template

from app.config import get_settings

log = logging.getLogger("ese.notifications")


@dataclass
class OutboundMessage:
    template_key: str
    recipient: str
    subject: str
    body: str
    payload: dict = field(default_factory=dict)


# In-memory outbox used by MAIL_BACKEND=memory (tests) — never enabled in production.
OUTBOX: list[OutboundMessage] = []

TEMPLATES: dict[str, tuple[str, str]] = {
    "auth.verify_email": (
        "Confirm your email address — European School of Economics",
        "Dear $name,\n\n"
        "Please confirm your email address to continue your application to the European School of "
        "Economics:\n\n$link\n\nThis link expires in $hours hours. If you did not create an account, "
        "you can ignore this message.\n\nAdmissions, European School of Economics",
    ),
    "auth.password_reset": (
        "Reset your password — European School of Economics",
        "Dear $name,\n\nA password reset was requested for your account. Use this link within "
        "$minutes minutes:\n\n$link\n\nIf you did not request this, no action is needed and your "
        "password is unchanged.\n\nEuropean School of Economics",
    ),
    "staff.account_created": (
        "Your staff account — International Student Recruitment & Compliance System",
        "Dear $name,\n\nAn account has been created for you with the role: $role.\n\n"
        "Set your password using this link within $hours hours:\n\n$link\n\n"
        "European School of Economics",
    ),
    "application.info_requested": (
        "More information needed — $ref",
        "Dear $name,\n\nWe have reviewed your application $ref and need a little more from you before we can "
        "continue:\n\n$reason\n\nSign in to update your application:\n\n$link\n\n"
        "Admissions, European School of Economics",
    ),
    "offer.issued": (
        "Your offer — $ref",
        "Dear $name,\n\nCongratulations. Following assessment of application $ref, the European School of "
        "Economics is pleased to make you a $offer_type offer for $programme ($intake).\n\n$conditions\n"
        "Sign in to read the offer letter and respond:\n\n$link\n\nAdmissions, European School of Economics",
    ),
    "cas.assigned": (
        "Your CAS has been assigned — $ref",
        "Dear $name,\n\nYour Confirmation of Acceptance for Studies (CAS) has been assigned.\n\n"
        "CAS number: $cas_number\nCourse: $course\nCourse dates: $start_date to $end_date\n\n"
        "Use this CAS number in your Student visa application. Keep it safe and check every detail carefully; "
        "tell us straight away if anything is wrong.\n\nEuropean School of Economics",
    ),
    "application.submitted": (
        "Application received — $ref",
        "Dear $name,\n\nThank you. Your application to the European School of Economics has been "
        "submitted.\n\nApplication reference: $ref\nProgramme: $programme\nIntake: $intake\n\n"
        "You can follow its progress at any time by signing in:\n\n$link\n\n"
        "Admissions, European School of Economics",
    ),
}


COMPLIANCE_ALERT_TEMPLATES: dict[str, tuple[str, str]] = {
    "event.created": (
        "Reportable event raised — $event",
        "A $event event has been raised for student record $student_id.\n\n"
        "It must be reported to UKVI through the Sponsorship Management System by $deadline.\n\n"
        "Open the reporting queue to action it.",
    ),
    "event.deadline_approaching": (
        "Reporting deadline approaching — $event",
        "The $event event for student record $student_id must be reported by $deadline.\n\n"
        "This is a reminder ahead of the deadline, not a report: the SMS reference must be recorded "
        "against the event once it has been submitted.",
    ),
    "event.escalated": (
        "REPORTING DEADLINE BREACHED — $event",
        "The $event event for student record $student_id passed its reporting deadline of $deadline "
        "without being reported.\n\nThis is escalated to the Authorising Officer. A late report is "
        "still required, and the breach is recorded in the audit trail.",
    ),
    "attendance.threshold_breached": (
        "Attendance below threshold — student $student_id",
        "Attendance for student record $student_id is $percentage% over the last 30 days, below the "
        "$threshold% engagement threshold.\n\nReview the record and decide whether to escalate.",
    ),
    "permission.expiring": (
        "Permission to stay expiring — student $student_id",
        "The permission recorded for student record $student_id expires on $expires.\n\n"
        "Check whether an extension or a new application is in progress.",
    ),
    "agent.review_due": (
        "Agent review $when — $agent",
        "The periodic review of recruitment agent $agent ($country) is due on $review_date.\n\n"
        "Check the agreement and due-diligence record are current and the agent's refusal rate is within "
        "its threshold, then record the new due-diligence and next review dates on the agent register:\n\n"
        "$link",
    ),
    "document.expiring": (
        "Document expiring — $category",
        "Document $document_id ($category) expires on $expires.\n\nRequest a replacement before it "
        "lapses so the file stays complete.",
    ),
    "calendar.expiring": (
        "Bank holiday calendar runs out in $days days",
        "The England and Wales bank holiday calendar in the compliance system reaches its last "
        "entry in $days days.\n\nWhen it runs out the system will refuse to calculate reporting "
        "deadlines rather than guess them, which stops status changes being recorded. Load the "
        "next year's dates from gov.uk before then.",
    ),
    "offer.expiring": (
        "Offer expiring without a response — offer $offer_id",
        "Offer $offer_id expires on $expires and the applicant has not responded.\n\n"
        "Follow up or let it lapse.",
    ),
}
TEMPLATES.update(COMPLIANCE_ALERT_TEMPLATES)


def render(template_key: str, payload: dict) -> tuple[str, str]:
    subject_t, body_t = TEMPLATES[template_key]
    return Template(subject_t).safe_substitute(payload), Template(body_t).safe_substitute(payload)


def send(template_key: str, recipient: str, payload: dict) -> OutboundMessage:
    subject, body = render(template_key, payload)
    msg = OutboundMessage(template_key, recipient, subject, body, payload)
    try:
        _deliver(msg)
    except Exception:  # noqa: BLE001 — a failed send must not undo a committed action
        log.exception("Failed to send %s to %s", template_key, recipient)
    return msg


def _deliver(msg: OutboundMessage) -> None:
    s = get_settings()
    backend = s.MAIL_BACKEND
    if backend == "memory":
        OUTBOX.append(msg)
    elif backend == "console" or not s.email_enabled:
        # Development and UAT never send, whatever MAIL_BACKEND says: the message goes to the log.
        log.warning("EMAIL (console backend) to=%s subject=%s\n%s", msg.recipient, msg.subject, msg.body)
    else:
        # Off the request thread: send latency must not reveal whether an account exists.
        threading.Thread(target=_send_smtp_logged, args=(msg,), daemon=True).start()


def _send_smtp_logged(msg: OutboundMessage) -> None:
    try:
        _send_smtp(msg)
    except Exception:  # noqa: BLE001
        log.exception("SMTP send failed for %s to %s", msg.template_key, msg.recipient)


def _send_smtp(msg: OutboundMessage) -> None:
    s = get_settings()
    em = EmailMessage()
    em["From"] = s.MAIL_FROM
    em["To"] = msg.recipient
    em["Subject"] = msg.subject
    em.set_content(msg.body)
    with smtplib.SMTP(s.SMTP_HOST or "localhost", s.SMTP_PORT, timeout=15) as smtp:
        if s.SMTP_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if s.SMTP_USER:
            smtp.login(s.SMTP_USER, s.SMTP_PASSWORD or "")
        smtp.send_message(em)


# --- Operational alerts: queued, deduplicated, dispatched by the worker (LLD 13) ---------------
def enqueue(
    db,  # noqa: ANN001 — Session; imported lazily to keep this module free of ORM imports at import time
    template_key: str,
    recipient: str,
    payload: dict,
    *,
    dedupe_key: str | None = None,
    application_id: int | None = None,
    student_id: int | None = None,
) -> bool:
    """Queue an alert. Returns False when an identical alert is already queued or sent, which is
    the normal outcome for a job running its second time in a day."""
    from sqlalchemy.exc import IntegrityError

    from app.models.operations import NotificationQueueRow

    subject, body = render(template_key, payload)
    row = NotificationQueueRow(
        template_key=template_key,
        recipient=recipient,
        payload={k: str(v) for k, v in payload.items()},
        dedupe_key=dedupe_key,
        application_id=application_id,
        student_id=student_id,
        subject=subject,
        body=body,
    )
    try:
        # The savepoint opens before the row is added, because begin_nested() flushes what is
        # already pending: a duplicate alert is the expected outcome of a job's second run in a
        # day, and must not roll back the alerts and escalations that run has already produced.
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        if row in db:
            db.expunge(row)
        return False
    return True


def dispatch_queued(db, limit: int = 100) -> int:  # noqa: ANN001 — Session
    """Send what is queued and record the outcome (FR-118). Run by the worker every few minutes."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.operations import (
        MAX_SEND_ATTEMPTS,
        NOTIFICATION_FAILED,
        NOTIFICATION_QUEUED,
        NOTIFICATION_SENT,
        NotificationQueueRow,
    )

    rows = db.scalars(
        select(NotificationQueueRow)
        .where(NotificationQueueRow.status == NOTIFICATION_QUEUED,
               NotificationQueueRow.send_after <= datetime.now(UTC))  # fmt: skip
        .order_by(NotificationQueueRow.id)
        .limit(limit)
    ).all()
    sent = 0
    for row in rows:
        row.attempts += 1
        try:
            _deliver(OutboundMessage(row.template_key, row.recipient, row.subject or "", row.body or "", {}))
            row.status = NOTIFICATION_SENT
            row.sent_at = datetime.now(UTC)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad address must not stall the queue
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if row.attempts >= MAX_SEND_ATTEMPTS:
                row.status = NOTIFICATION_FAILED
        db.flush()
    return sent


def compliance_inbox(db) -> str:  # noqa: ANN001 — Session
    """Where compliance alerts go until per-user routing arrives (FR-99)."""
    from app.services.settings import get_setting

    return str(get_setting(db, "COMPLIANCE_INBOX", "compliance@ese.example"))


def authorising_officer(db) -> str:  # noqa: ANN001 — Session
    from app.services.settings import get_setting

    return str(get_setting(db, "AUTHORISING_OFFICER_EMAIL", "authorising-officer@ese.example"))


def link(path: str) -> str:
    return get_settings().PUBLIC_BASE_URL.rstrip("/") + path
