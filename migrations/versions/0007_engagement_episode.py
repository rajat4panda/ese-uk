"""The engagement alert remembers the breach, not the calendar.

Revision ID: 0007_engagement_episode
Revises: 0006_offer_pending_status
Create Date: 2026-09-27

Migration 0005 stopped the attendance alert repeating daily by holding off for a configured number
of days after the last alert. A review found that hold-off is too blunt: it is keyed on time
alone, so a student who breaches, recovers, and breaches again inside the window gets no second
alert. For FR-78 that is a missed engagement breach on a sponsored student — the opposite failure
to the one 0005 fixed, and the more serious of the two.

The hold-off now belongs to the breach rather than to the clock. student_records carries the
moment the current breach was alerted; recovering above the threshold clears it, so the next
breach is a new one and speaks immediately.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

from alembic import op

revision = "0007_engagement_episode"
down_revision = "0006_offer_pending_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE student_records ADD COLUMN engagement_alerted_at timestamptz NULL")
    op.execute(
        "COMMENT ON COLUMN student_records.engagement_alerted_at IS "
        "'When the current attendance breach was last alerted; NULL once the student recovers (FR-78)'"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
