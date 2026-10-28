"""Audit writer (LLD Section 10).

Called inside the caller's transaction, never after commit, so an action and its evidence share
a fate. The action vocabulary is closed: an action not listed here is refused, and a test
asserts that every action the code writes is in this list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.audit import AuditEntry

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.user import User

# LLD 10 vocabulary.
LLD_ACTIONS: frozenset[str] = frozenset(
    {
        # auth
        "auth.register", "auth.login", "auth.login_failed", "auth.locked", "auth.password_reset",
        "auth.mfa_enrolled",
        # application
        "application.create", "application.save", "application.submit", "application.withdraw",
        "application.info_requested",
        # document
        "document.upload", "document.verify", "document.view", "document.quarantine", "document.supersede",
        # academic
        "academic.save", "academic.complete",
        # offer
        "offer.create", "offer.issue", "offer.condition_met", "offer.response", "offer.withdraw", "offer.override",
        # compliance
        "compliance.save", "compliance.complete", "compliance.return",
        # cas
        "cas.assign", "cas.deposit_override", "cas.allocation_warning", "cas.withdraw", "cas.export",
        # visa
        "visa.outcome_recorded",
        # student
        "student.enrol", "student.attendance", "student.status_change", "student.escalation",
        "student.status_check",
        # finance
        "finance.payment", "finance.scholarship", "finance.refund", "finance.schedule_change",
        # reporting
        "event.created_auto", "event.created_manual", "event.reported", "event.escalated",
        # admin
        "admin.user_create", "admin.user_update", "admin.user_role_change", "admin.user_deactivate",
        "admin.config_change",
        # data
        "data.evidence_pack", "data.subject_access", "data.erasure", "data.destruction", "data.import",
    }
)  # fmt: skip

# Additions made in the foundation build, proposed for LLD v1.2 and flagged for Finteam review.
PROPOSED_ACTIONS: frozenset[str] = frozenset(
    {
        "auth.email_verified",  # Appendix A email verification
        "auth.logout",  # explicit sign-out revokes the token
        "admin.user_reactivate",  # reversal of admin.user_deactivate
        "compliance.second_line",  # FR-52 second-line review, distinct from an ordinary save
        # BRD FR-90 to FR-94: the agent register is changed in the system, so the changes are audited
        "agent.create", "agent.update", "agent.status_change",
        # Prototype screen 12: a case note on the student record
        "student.note",
        # FR-76: evidence of a report attached to, or replaced on, a reportable event
        "event.evidence",
        # BRD FR-67 and FR-61: contact changes and enrolment evidence on the student record
        "student.contact_change", "student.enrolment_evidence",
    }
)

ACTIONS: frozenset[str] = LLD_ACTIONS | PROPOSED_ACTIONS


class UnknownAuditAction(ValueError):
    pass


def write(
    db: Session,
    actor: User | None,
    action: str,
    *,
    application: Application | None = None,
    application_id: int | None = None,
    student_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: str = "",
    ip: str | None = None,
    actor_label: str | None = None,
) -> AuditEntry:
    """Add an audit entry to the current transaction. No commit here — the caller owns it."""
    if action not in ACTIONS:
        raise UnknownAuditAction(f"'{action}' is not in the audit vocabulary (LLD 10)")
    entry = AuditEntry(
        user_id=actor.id if actor else None,
        user_name=actor.full_name if actor else (actor_label or "system"),
        user_role=str(actor.role) if actor else ("anonymous" if actor_label else "system"),
        action=action,
        application_id=application.id if application is not None else application_id,
        student_id=student_id,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail[:4000],
        ip_address=ip,
    )
    db.add(entry)
    db.flush([entry])
    return entry
