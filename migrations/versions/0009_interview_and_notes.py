"""Credibility interview on the academic assessment, and case notes on the student record.

Revision ID: 0009_interview_and_notes
Revises: 0008_agent_management
Create Date: 2026-09-30

Prototype screen 6 has "Interview completed" and "Interview notes uploaded" in the academic
checklist, and the BRD asks for the credibility interview to be recorded (FR-37). The ticks existed;
what the interview found did not. academic_assessments now records who interviewed, when, and the
outcome, and a new document category holds the notes themselves.

Prototype screen 12 has a Notes tab on the student record. student_notes is append-only, like the
audit trail: a note is corrected by adding another one, never edited, because a case note is
evidence of what the sponsor knew and when.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

from alembic import op

revision = "0009_interview_and_notes"
down_revision = "0008_agent_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'interview_notes'")
    op.execute(
        """
        ALTER TABLE academic_assessments
            ADD COLUMN interview_date date NULL,
            ADD COLUMN interviewer text NULL,
            ADD COLUMN interview_outcome text NULL,
            ADD COLUMN interview_notes text NULL
        """
    )
    op.execute(
        "ALTER TABLE academic_assessments ADD CONSTRAINT ck_academic_interview_outcome "
        "CHECK (interview_outcome IS NULL OR interview_outcome IN ('satisfactory', 'concerns', 'unsatisfactory'))"
    )
    op.execute(
        """
        CREATE TABLE student_notes (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id  bigint      NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            body        text        NOT NULL CHECK (length(body) BETWEEN 3 AND 4000),
            author_id   bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("CREATE INDEX ix_student_notes_student ON student_notes (student_id, created_at DESC)")
    # Append-only, like audit_entries: the application may add a note and read notes, nothing else.
    op.execute("GRANT SELECT, INSERT ON student_notes TO app_role")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
