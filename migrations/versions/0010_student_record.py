"""The student record before the first intake: right-to-study checks, contact history, CAS withdrawal.

Revision ID: 0010_student_record
Revises: 0009_interview_and_notes
Create Date: 2026-10-28

BRD FR-68 (Must) asks for the right-to-study and immigration status checks themselves to be recorded:
who checked, when, how and what they found. Until now the system held one date, last_status_check,
which was stamped with today's date whenever a status changed — a date with no check behind it.
status_checks holds each check; last_status_check now means the date of the latest one. The dates
already stamped have no check behind them, so they are cleared: the record says "never checked" until
a real check is recorded, rather than claiming one that was never made.

BRD FR-67 asks for current contact details and changes of address with effective dates. The address
was copied once from the application at CAS and never changed. student_contact_changes holds each
change; the student record keeps the current values for screens and alerts.

BRD FR-58 asks for CAS withdrawal or cancellation with reason and date. cas now records which it was
and the date it took effect, which can be earlier than the day it is recorded.

Both new tables are append-only for the application role, like the audit trail: a check or a change
of address is corrected by recording another one, never by editing the first.

Forward-only (LLD 19.1).
"""

from __future__ import annotations

from alembic import op

revision = "0010_student_record"
down_revision = "0009_interview_and_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE status_checks (
            id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id            bigint      NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            check_date            date        NOT NULL,
            method                text        NOT NULL
                                  CHECK (method IN ('evisa_share_code', 'brp', 'passport_vignette', 'other')),
            reference             text        NULL,
            permission_type       text        NULL,
            permission_expiry     date        NULL,
            outcome               text        NOT NULL CHECK (outcome IN ('confirmed', 'discrepancy', 'no_permission')),
            notes                 text        NULL,
            evidence_document_id  bigint      NULL REFERENCES documents(id) ON DELETE RESTRICT,
            checked_by            bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT ck_status_check_confirmed_has_expiry
                CHECK (outcome <> 'confirmed' OR permission_expiry IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ix_status_checks_student ON status_checks (student_id, check_date DESC, id DESC)")

    op.execute(
        """
        CREATE TABLE student_contact_changes (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id      bigint      NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            effective_from  date        NOT NULL,
            address         jsonb       NOT NULL,
            phone           text        NULL,
            email           text        NULL,
            reason          text        NULL,
            recorded_by     bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_contact_changes_student ON student_contact_changes (student_id, effective_from DESC, id DESC)"
    )

    op.execute("GRANT SELECT, INSERT ON status_checks, student_contact_changes TO app_role")
    op.execute("UPDATE student_records SET last_status_check = NULL")

    # Current contact details held on the record, filled from the application the record came from.
    op.execute("ALTER TABLE student_records ADD COLUMN contact_phone text NULL, ADD COLUMN contact_email text NULL")
    op.execute(
        """
        UPDATE student_records s
           SET contact_phone = a.data->>'mobile', contact_email = a.data->>'email'
          FROM applications a
         WHERE a.id = s.application_id
        """
    )

    op.execute(
        "ALTER TABLE cas ADD COLUMN withdrawal_date date NULL, ADD COLUMN withdrawal_type text NULL "
        "CHECK (withdrawal_type IS NULL OR withdrawal_type IN ('withdrawn', 'cancelled'))"
    )
    # Withdrawals recorded before this migration were dated the day they were recorded.
    op.execute(
        """
        UPDATE cas c SET withdrawal_type = 'withdrawn',
               withdrawal_date = COALESCE(
                   (SELECT (a.at AT TIME ZONE 'Europe/London')::date FROM audit_entries a
                     WHERE a.action = 'cas.withdraw' AND a.entity_type = 'cas' AND a.entity_id = c.id
                     ORDER BY a.at LIMIT 1),
                   (c.assigned_at AT TIME ZONE 'Europe/London')::date)
         WHERE c.status = 'withdrawn'
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
