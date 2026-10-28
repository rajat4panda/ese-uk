"""Agent management: evidence held against an agent, and the reason for its status.

Revision ID: 0008_agent_management
Revises: 0007_engagement_episode
Create Date: 2026-09-30

BRD FR-90 to FR-94. The agents table has carried agreement_document_id and due_diligence_document_id
since the baseline, but a document could only belong to an application or a student, so there was
nothing those columns could point at. Documents may now belong to an agent instead, under two new
categories kept apart from the applicant ones.

status_reason records why an agent was suspended, put under review or deactivated. The audit trail
has the full history; this column is what the register shows today, so nobody has to go to the
trail to learn why an agent cannot be selected. updated_at is kept by the same trigger function the
applications table uses.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_agent_management"
down_revision = "0007_engagement_episode"
branch_labels = None
depends_on = None

#: Settings this migration adds. The test suite re-seeds from here after each test.
SETTINGS = [("AGENT_REVIEW_ALERT_DAYS", "30", "int")]


def upgrade() -> None:
    op.execute("ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'agent_agreement'")
    op.execute("ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'agent_due_diligence'")

    op.execute("ALTER TABLE documents ADD COLUMN agent_id bigint NULL REFERENCES agents(id) ON DELETE RESTRICT")
    op.execute("CREATE INDEX ix_documents_agent ON documents (agent_id) WHERE agent_id IS NOT NULL")
    op.execute("ALTER TABLE documents DROP CONSTRAINT ck_documents_owner")
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT ck_documents_owner "
        "CHECK (application_id IS NOT NULL OR student_id IS NOT NULL OR agent_id IS NOT NULL)"
    )

    op.execute("ALTER TABLE agents ADD COLUMN status_reason text NULL")
    op.execute("ALTER TABLE agents ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()")
    op.execute(
        "CREATE TRIGGER trg_agents_updated_at BEFORE UPDATE ON agents "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "COMMENT ON COLUMN agents.status_reason IS "
        "'Why the agent is under review, suspended or deactivated (FR-94); history is in audit_entries'"
    )
    # One agent per trading name and country: the application form's picker shows name and country
    # only, so two identical rows could not be told apart. Created only if existing data allows it;
    # otherwise the service-level check still applies and the duplicates must be merged by hand.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM agents GROUP BY lower(name), country HAVING count(*) > 1) THEN
                CREATE UNIQUE INDEX ux_agents_name_country ON agents (lower(name), country);
            ELSE
                RAISE WARNING 'agents has duplicate name/country rows; ux_agents_name_country not created';
            END IF;
        END $$;
        """
    )
    # How far ahead the agent_review_reminder job starts chasing a review (FR-91).
    for key, value, value_type in SETTINGS:
        op.execute(
            sa.text(
                "INSERT INTO system_settings (key, value, value_type) VALUES (:k, :v, :t) ON CONFLICT (key) DO NOTHING"
            ).bindparams(k=key, v=value, t=value_type)
        )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
