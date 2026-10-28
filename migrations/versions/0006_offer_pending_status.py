"""An application that has passed academic review but has no offer yet has a status of its own.

Revision ID: 0006_offer_pending_status
Revises: 0005_evidence_and_corrections
Create Date: 2026-09-27

FR-35 asks that completing the academic assessment with "Recommend for Offer" moves the
application on. It did not: the record sat in Academic Review until somebody built an offer, so
the pipeline could not distinguish an application nobody had looked at from one that had been
approved and was waiting for an offer letter. Those are different queues for different people.

'offer_pending' is added to the application_status enum rather than carried as a flag, because
every other stage of this workflow is a status and the audit trail records the status at the time
of each entry. Historic rows are untouched: nothing is migrated into the new value, so an
application that reached Offer Issued last week still reads as having gone straight there.

Adding a value to a Postgres enum cannot run inside a transaction block in older versions and
cannot be removed at all, which is another reason this is a decision taken deliberately rather
than a convenience.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

from alembic import op

revision = "0006_offer_pending_status"
down_revision = "0005_evidence_and_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AFTER 'academic_review' keeps the enum's declaration order matching the workflow order, which
    # is what any ORDER BY status in a report will follow.
    op.execute("ALTER TYPE application_status ADD VALUE IF NOT EXISTS 'offer_pending' AFTER 'academic_review'")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
