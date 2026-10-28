"""Admissions, offers, compliance review, finance and CAS — the R1a control chain.

Revision ID: 0003_workflow_to_cas
Revises: 0002_auth_operational
Create Date: 2026-09-25

Implements LLD v1.1 Sections 3.4 (academic_assessments, compliance_reviews), 3.5 (offers,
offer_conditions), 3.6 (cas, visa_outcomes), 3.7 (student_records), 3.9 (fee_schedules,
application_fees, payments) and the 3.12 reference tables the workflow reads
(checklist_definitions, offer_condition_library, cas_allocations).

It also completes the two deferred foreign keys from 0001: documents.student_id and
audit_entries.student_id now reference student_records(id).

Forward-only (LLD 19.1).
"""

from alembic import op

revision = "0003_workflow_to_cas"
down_revision = "0002_auth_operational"
branch_labels = None
depends_on = None

# Checklist items (BRD v1.1 FR-27/FR-44; LLD 3.12 checklist_definitions).
ACADEMIC_ITEMS = [
    ("qualifications_verified", "Qualifications verified against original or certified documents", True, 10),
    ("entry_requirements_met", "Entry requirements for the programme are satisfied", True, 20),
    ("progression_appropriate", "Academic progression is appropriate", True, 30),
    ("english_evidence_verified", "English language evidence verified against the accepted standard", True, 40),
    ("interview_completed", "Credibility interview completed", False, 50),
    ("interview_notes_uploaded", "Interview notes uploaded to the record", False, 60),
]
COMPLIANCE_ITEMS = [
    ("identity_passport_verified", "Identity and passport verified", True, 10),
    ("previous_uk_study_checked", "Previous UK study and academic progression checked", True, 20),
    ("immigration_history_checked", "Immigration history and any previous refusals checked", True, 30),
    ("english_evidence_verified", "English language evidence meets the requirement", True, 40),
    ("credibility_assessed", "Credibility assessed and recorded", True, 50),
    ("financial_status_checked", "Financial and fee status checked", True, 60),
    ("documents_complete", "Required documents are complete and on file", True, 70),
]
# Standard offer conditions (LLD 3.12 offer_condition_library, FR-36).
CONDITIONS = [
    ("Provide certified final transcripts and award certificate", "academic"),
    ("Provide the original English language test certificate", "english"),
    ("Pay the tuition fee deposit in full", "financial"),
    ("Provide evidence of funds in line with UKVI requirements", "financial"),
    ("Provide a clear copy of the passport biographical page", "identity"),
    ("Complete a credibility interview with ESE", "credibility"),
]


def upgrade() -> None:
    # --- 3.12 reference tables the workflow reads ------------------------------------------
    op.execute(
        """
        CREATE TABLE checklist_definitions (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            checklist_type text    NOT NULL CHECK (checklist_type IN ('academic', 'compliance')),
            item_key       text    NOT NULL,
            label          text    NOT NULL,
            mandatory      boolean NOT NULL DEFAULT true,
            sort_order     int     NOT NULL DEFAULT 0,
            active         boolean NOT NULL DEFAULT true,
            UNIQUE (checklist_type, item_key)
        )
        """
    )
    rows = [("academic", *i) for i in ACADEMIC_ITEMS] + [("compliance", *i) for i in COMPLIANCE_ITEMS]
    values = ", ".join(
        f"('{t}', '{k}', '{lab}', {str(m).lower()}, {o})" for t, k, lab, m, o in rows
    )
    op.execute(  # noqa: S608 — constants defined above
        f"INSERT INTO checklist_definitions (checklist_type, item_key, label, mandatory, sort_order) VALUES {values}"
    )

    op.execute(
        """
        CREATE TABLE offer_condition_library (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            description text    NOT NULL,
            category    text    NOT NULL,
            active      boolean NOT NULL DEFAULT true
        )
        """
    )
    lib = ", ".join(f"('{d}', '{c}')" for d, c in CONDITIONS)
    op.execute(f"INSERT INTO offer_condition_library (description, category) VALUES {lib}")  # noqa: S608

    op.execute(
        """
        CREATE TABLE cas_allocations (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            period_label    text    NOT NULL UNIQUE,
            period_start    date    NOT NULL,
            period_end      date    NOT NULL,
            allocated_count int     NOT NULL CHECK (allocated_count >= 0),
            status          text    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            CHECK (period_end > period_start)
        )
        """
    )
    op.execute("CREATE INDEX ix_cas_allocations_period ON cas_allocations (period_start, period_end)")

    # --- 3.4 academic_assessments and compliance_reviews -------------------------------------
    for table, extra in (
        ("academic_assessments", "outcome text NULL CHECK (outcome IN ('recommend_offer','request_info','reject')),"),
        (
            "compliance_reviews",
            "risk_rating risk_rating NULL, atas_required boolean NULL, atas_reference text NULL, "
            "second_line_by bigint NULL REFERENCES users(id) ON DELETE RESTRICT, "
            "second_line_at timestamptz NULL, second_line_outcome text NULL,",
        ),
    ):
        op.execute(
            f"""
            CREATE TABLE {table} (
                id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                application_id bigint      NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
                checklist      jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                {extra}
                comments       text        NOT NULL DEFAULT '',
                completed_by   bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
                completed_at   timestamptz NULL,
                created_at     timestamptz NOT NULL DEFAULT now()
            )
            """
        )

    # --- 3.5 offers and offer_conditions ------------------------------------------------------
    op.execute(
        """
        CREATE TABLE offers (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id     bigint      NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
            offer_type         text        NOT NULL CHECK (offer_type IN ('conditional', 'unconditional')),
            letter_storage_key text        NULL,
            issued_at          timestamptz NULL,
            issued_by          bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            issue_method       text        NULL CHECK (issue_method IN ('email', 'portal', 'post')),
            response           text        NULL CHECK (response IN ('accepted', 'declined', 'lapsed', 'withdrawn')),
            response_at        timestamptz NULL,
            expiry_date        date        NULL,
            override_by        bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            override_reason    text        NULL,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE offer_conditions (
            id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            offer_id             bigint      NOT NULL REFERENCES offers(id) ON DELETE RESTRICT,
            description          text        NOT NULL,
            status               text        NOT NULL DEFAULT 'outstanding'
                                 CHECK (status IN ('outstanding', 'met')),
            evidence_document_id bigint      NULL REFERENCES documents(id) ON DELETE RESTRICT,
            verified_by          bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            verified_at          timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_offer_conditions_offer ON offer_conditions (offer_id, status)")

    # --- 3.9 finance -------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE fee_schedules (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            programme_id    bigint        NOT NULL REFERENCES programmes(id) ON DELETE RESTRICT,
            intake_id       bigint        NOT NULL REFERENCES intakes(id) ON DELETE RESTRICT,
            tuition_fee     numeric(10,2) NOT NULL CHECK (tuition_fee >= 0),
            minimum_deposit numeric(10,2) NOT NULL DEFAULT 0 CHECK (minimum_deposit >= 0),
            currency        char(3)       NOT NULL DEFAULT 'GBP',
            effective_from  date          NOT NULL,
            effective_to    date          NULL,
            created_by      bigint        NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at      timestamptz   NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_fee_schedule_current ON fee_schedules (programme_id, intake_id) "
        "WHERE effective_to IS NULL"
    )
    op.execute(
        """
        CREATE TABLE application_fees (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id   bigint        NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
            fee_schedule_id  bigint        NOT NULL REFERENCES fee_schedules(id) ON DELETE RESTRICT,
            agreed_fee       numeric(10,2) NOT NULL CHECK (agreed_fee >= 0),
            variation_reason text          NULL,
            set_by           bigint        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            set_at           timestamptz   NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE payments (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id  bigint        NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
            type            payment_type  NOT NULL,
            amount          numeric(10,2) NOT NULL CHECK (amount > 0),
            payment_date    date          NOT NULL,
            method          text          NULL CHECK (method IN ('bank_transfer', 'card', 'cash', 'waiver')),
            reference       text          NULL,
            provider_txn_id text          NULL UNIQUE,
            reason          text          NULL,
            authorised_by   bigint        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            recorded_at     timestamptz   NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("CREATE INDEX ix_payments_application ON payments (application_id, payment_date)")

    # --- 3.6 cas and visa_outcomes -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE cas (
            id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id       bigint        NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
            cas_number           text          NOT NULL UNIQUE CHECK (cas_number ~ '^[A-Z0-9]{10,20}$'),
            course_title         text          NOT NULL,
            start_date           date          NOT NULL,
            end_date             date          NOT NULL,
            tuition_fee          numeric(10,2) NOT NULL CHECK (tuition_fee >= 0),
            amount_paid          numeric(10,2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
            level1_user          text          NOT NULL,
            authorised_by        bigint        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            allocation_period_id bigint        NOT NULL REFERENCES cas_allocations(id) ON DELETE RESTRICT,
            status               text          NOT NULL DEFAULT 'assigned'
                                 CHECK (status IN ('assigned', 'withdrawn', 'cancelled')),
            withdrawal_reason    text          NULL,
            notes                text          NOT NULL DEFAULT '',
            assigned_at          timestamptz   NOT NULL DEFAULT now(),
            CHECK (end_date > start_date)
        )
        """
    )
    op.execute("CREATE INDEX ix_cas_allocation ON cas (allocation_period_id, status)")
    op.execute(
        """
        CREATE TABLE visa_outcomes (
            id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            cas_id                  bigint       NOT NULL UNIQUE REFERENCES cas(id) ON DELETE RESTRICT,
            outcome                 visa_outcome NOT NULL,
            decision_date           date         NOT NULL,
            refusal_reason_category text         NULL,
            evidence_document_id    bigint       NULL REFERENCES documents(id) ON DELETE RESTRICT,
            recorded_by             bigint       NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            recorded_at             timestamptz  NOT NULL DEFAULT now()
        )
        """
    )

    # --- 3.7 student_records (created as an effect of CAS assignment) -------------------------
    op.execute(
        """
        CREATE TABLE student_records (
            id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id        bigint           NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
            cas_id                bigint           NOT NULL REFERENCES cas(id) ON DELETE RESTRICT,
            enrolment_status      enrolment_status NOT NULL DEFAULT 'awaiting_arrival',
            enrolment_status_reason text           NULL,
            enrolment_date        date             NULL,
            enrolment_evidence_id bigint           NULL REFERENCES documents(id) ON DELETE RESTRICT,
            permission_expiry     date             NULL,
            last_status_check     date             NULL,
            completion_outcome    text             NULL CHECK (completion_outcome IN ('completed', 'not_completed')),
            completion_date       date             NULL,
            current_address       jsonb            NOT NULL DEFAULT '{}'::jsonb,
            created_at            timestamptz      NOT NULL DEFAULT now(),
            updated_at            timestamptz      NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_student_records_status ON student_records (enrolment_status)")
    op.execute(
        "CREATE TRIGGER trg_student_records_updated_at BEFORE UPDATE ON student_records "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # Deferred foreign keys from 0001, now that student_records exists.
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT fk_documents_student "
        "FOREIGN KEY (student_id) REFERENCES student_records(id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE audit_entries ADD CONSTRAINT fk_audit_student "
        "FOREIGN KEY (student_id) REFERENCES student_records(id) ON DELETE RESTRICT"
    )

    # --- Grants (least privilege: no DELETE anywhere; audit stays append-only) ----------------
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON checklist_definitions, offer_condition_library, cas_allocations, "
        "academic_assessments, compliance_reviews, offers, offer_conditions, fee_schedules, application_fees, "
        "payments, cas, visa_outcomes, student_records TO app_role"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
