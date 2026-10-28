"""English evidence recorded on the academic assessment, and the engagement alert hold-off.

Revision ID: 0005_evidence_and_corrections
Revises: 0004_post_cas_compliance
Create Date: 2026-09-26

Two small changes, both closing gaps found by the requirements assessment of 26 September.

FR-36 asks that the English language evidence be *verified*, and a tick alone does not say what
was verified. A sponsor asked at a compliance visit which evidence it accepted for a given student,
and against what standard, must be able to answer from the record rather than from memory, so the
academic assessment now carries both, beside the tick that was already there.

The second change is a settings row. The engagement alert deduplicated on a window that moved by a
day every day, so a student who stayed below the attendance threshold produced a fresh alert every
morning and indefinitely. The hold-off between repeats is now a configured number of days rather
than an accident of the window arithmetic.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

from alembic import op

revision = "0005_evidence_and_corrections"
down_revision = "0004_post_cas_compliance"
branch_labels = None
depends_on = None

#: The routes the application form offers (schemas/application.py EnglishRoute), plus the case
#: where no evidence is needed. Held as a CHECK rather than an enum: this list is a policy
#: position that ESE may revise, and a CHECK is far cheaper to change than a Postgres enum.
ENGLISH_EVIDENCE = (
    "majority_english_country",
    "degree_taught_in_english",
    "slt_test",
    "provider_assessment",
    "not_required",
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE academic_assessments
            ADD COLUMN english_evidence text NULL,
            ADD COLUMN english_standard text NULL
        """
    )
    op.execute(
        "ALTER TABLE academic_assessments ADD CONSTRAINT ck_academic_english_evidence "
        "CHECK (english_evidence IS NULL OR english_evidence IN "
        f"({', '.join(chr(39) + v + chr(39) for v in ENGLISH_EVIDENCE)}))"
    )
    # The standard is free text because it names a real-world threshold — "CEFR B2", "IELTS UKVI
    # 6.0 with no band below 5.5" — and those change without our release cycle.
    op.execute(
        "COMMENT ON COLUMN academic_assessments.english_standard IS "
        "'The standard the evidence was checked against, as the assessor recorded it (FR-36)'"
    )

    # Days before an unresolved attendance breach alerts the compliance inbox again.
    op.execute(
        """
        INSERT INTO system_settings (key, value, value_type)
        VALUES ('ENGAGEMENT_ALERT_REPEAT_DAYS', '14', 'int')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
