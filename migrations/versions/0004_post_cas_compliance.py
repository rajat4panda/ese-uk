"""Post-CAS compliance: attendance, reportable events, escalations and the operational tables.

Revision ID: 0004_post_cas_compliance
Revises: 0003_workflow_to_cas
Create Date: 2026-09-26

Implements LLD v1.1 Sections 3.7 (attendance_records, escalations), 3.8 (reportable_events),
3.12 (bank_holidays) and 3.13 (notification_queue, job_runs, export_jobs).

Two indexes in here carry compliance weight rather than performance:

  * ix_events_dedupe makes automatic event creation idempotent, so a scheduled job that runs twice
    cannot raise the same non-enrolment event twice (LLD 3.8, T-13).
  * ix_attendance_unique stops the same session being recorded twice for a student, which would
    quietly distort the engagement percentage that FR-75 and FR-78 act on.

bank_holidays is seeded with England and Wales dates to the end of 2028. The deadline calculation
raises rather than guessing when it runs past the end of the calendar (LLD 8.2), so the table being
maintained is itself a compliance duty.

Forward-only (LLD 19.1).
"""

from alembic import op

revision = "0004_post_cas_compliance"
down_revision = "0003_workflow_to_cas"
branch_labels = None
depends_on = None

# England and Wales bank holidays, gov.uk. Substitute days are the dates actually observed.
BANK_HOLIDAYS = [
    ("2026-01-01", "New Year's Day"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-06", "Easter Monday"),
    ("2026-05-04", "Early May bank holiday"),
    ("2026-05-25", "Spring bank holiday"),
    ("2026-08-31", "Summer bank holiday"),
    ("2026-12-25", "Christmas Day"),
    ("2026-12-28", "Boxing Day (substitute day)"),
    ("2027-01-01", "New Year's Day"),
    ("2027-03-26", "Good Friday"),
    ("2027-03-29", "Easter Monday"),
    ("2027-05-03", "Early May bank holiday"),
    ("2027-05-31", "Spring bank holiday"),
    ("2027-08-30", "Summer bank holiday"),
    ("2027-12-27", "Christmas Day (substitute day)"),
    ("2027-12-28", "Boxing Day (substitute day)"),
    ("2028-01-03", "New Year's Day (substitute day)"),
    ("2028-04-14", "Good Friday"),
    ("2028-04-17", "Easter Monday"),
    ("2028-05-01", "Early May bank holiday"),
    ("2028-05-29", "Spring bank holiday"),
    ("2028-08-28", "Summer bank holiday"),
    ("2028-12-25", "Christmas Day"),
    ("2028-12-26", "Boxing Day"),
]

# Escalation steps (FR-79). Held as a check constraint rather than an enum: the ladder is a
# procedural choice ESE may change, unlike the reportable event types, which are UKVI's.
ESCALATION_STEPS = ("email", "meeting", "formal_warning", "final_warning", "reported")


def upgrade() -> None:
    # --- 3.12 bank_holidays -------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE bank_holidays (
            holiday_date date NOT NULL,
            name         text NOT NULL,
            jurisdiction text NOT NULL DEFAULT 'england-and-wales',
            PRIMARY KEY (holiday_date, jurisdiction)
        )
        """
    )
    values = ", ".join(f"('{d}', '{n.replace(chr(39), chr(39) * 2)}')" for d, n in BANK_HOLIDAYS)
    op.execute(f"INSERT INTO bank_holidays (holiday_date, name) VALUES {values}")

    # --- 3.7 attendance_records ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE attendance_records (
            id             bigint            GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id     bigint            NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            session_date   date              NOT NULL,
            module         text              NOT NULL,
            status         attendance_status NOT NULL,
            absence_reason text              NULL,
            approved_by    bigint            NULL REFERENCES users(id) ON DELETE RESTRICT,
            recorded_by    bigint            NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            recorded_at    timestamptz       NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ix_attendance_unique ON attendance_records (student_id, session_date, module)")
    op.execute("CREATE INDEX ix_attendance_student_date ON attendance_records (student_id, session_date DESC)")

    # --- 3.8 reportable_events ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE reportable_events (
            id                   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id           bigint      NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            event_type           event_type  NOT NULL,
            event_date           date        NOT NULL,
            deadline_date        date        NOT NULL,
            source               text        NOT NULL CHECK (source IN ('automatic', 'manual')),
            detail               text        NOT NULL DEFAULT '',
            owner_id             bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            status               text        NOT NULL DEFAULT 'pending'
                                             CHECK (status IN ('pending', 'reported', 'not_required')),
            reported_by          bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            reported_at          timestamptz NULL,
            reported_date        date        NULL,
            sms_reference        text        NULL,
            not_required_reason  text        NULL,
            evidence_document_id bigint      NULL REFERENCES documents(id) ON DELETE RESTRICT,
            escalated_at         timestamptz NULL,
            created_at           timestamptz NOT NULL DEFAULT now(),
            CHECK (deadline_date >= event_date),
            CHECK (status <> 'reported' OR (sms_reference IS NOT NULL AND reported_by IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_events_open_deadline ON reportable_events (deadline_date) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_events_dedupe ON reportable_events (student_id, event_type, event_date) "
        "WHERE source = 'automatic'"
    )
    op.execute("CREATE INDEX ix_events_student ON reportable_events (student_id, created_at DESC)")

    # --- 3.7 escalations ----------------------------------------------------------------------
    steps = ", ".join(f"'{s}'" for s in ESCALATION_STEPS)
    op.execute(
        f"""
        CREATE TABLE escalations (
            id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id  bigint      NOT NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            step        text        NOT NULL CHECK (step IN ({steps})),
            notes       text        NOT NULL DEFAULT '',
            raised_by   bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            raised_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_escalations_student ON escalations (student_id, raised_at DESC)")

    # --- 3.13 notification_queue (FR-115, FR-118) ---------------------------------------------
    op.execute(
        """
        CREATE TABLE notification_queue (
            id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            template_key   text        NOT NULL,
            recipient      text        NOT NULL,
            payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            application_id bigint      NULL REFERENCES applications(id) ON DELETE RESTRICT,
            student_id     bigint      NULL REFERENCES student_records(id) ON DELETE RESTRICT,
            dedupe_key     text        NULL UNIQUE,
            status         text        NOT NULL DEFAULT 'queued'
                                       CHECK (status IN ('queued', 'sent', 'failed')),
            attempts       int         NOT NULL DEFAULT 0,
            last_error     text        NULL,
            subject        text        NULL,
            body           text        NULL,
            send_after     timestamptz NOT NULL DEFAULT now(),
            sent_at        timestamptz NULL,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_notification_due ON notification_queue (send_after) WHERE status = 'queued'"
    )

    # --- 3.13 job_runs ------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE job_runs (
            id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_name         text        NOT NULL,
            started_at       timestamptz NOT NULL DEFAULT now(),
            finished_at      timestamptz NULL,
            outcome          text        NULL CHECK (outcome IN ('success', 'failed')),
            records_affected int         NOT NULL DEFAULT 0,
            error_detail     text        NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_job_runs_name_started ON job_runs (job_name, started_at DESC)")

    # --- 3.13 export_jobs ---------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE export_jobs (
            id                 uuid        PRIMARY KEY,
            job_type           text        NOT NULL
                                           CHECK (job_type IN ('evidence_pack', 'subject_access', 'bulk_import')),
            subject_ref        text        NULL,
            application_id     bigint      NULL REFERENCES applications(id) ON DELETE RESTRICT,
            requested_by       bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            status             text        NOT NULL DEFAULT 'pending'
                                           CHECK (status IN ('pending', 'running', 'ready', 'failed')),
            result_storage_key text        NULL,
            result_filename    text        NULL,
            expires_at         timestamptz NULL,
            error_detail       text        NULL,
            created_at         timestamptz NOT NULL DEFAULT now(),
            finished_at        timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_export_jobs_pending ON export_jobs (created_at) WHERE status = 'pending'")

    # --- Grants (least privilege: no DELETE anywhere; audit stays append-only) ------------------
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON attendance_records, reportable_events, escalations, job_runs, "
        "export_jobs, notification_queue TO app_role"
    )
    op.execute("GRANT SELECT ON bank_holidays TO app_role")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
