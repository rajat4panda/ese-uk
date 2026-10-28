"""Baseline schema: enumerations, identity, applications, documents, immutable audit.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-21

Implements LLD v1.1 Sections 3.1, 3.2, 3.3, 3.10, 3.11 (agents), 3.12 (programmes, intakes)
and Section 4. Forward-only (LLD 19.1).

Grants follow least privilege: app_role receives SELECT, INSERT and UPDATE on operational
tables and no DELETE anywhere (records are never soft- or hard-deleted, LLD 1.1). On
audit_entries it receives INSERT and SELECT only (LLD 3.10, NFR-12).
"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# Frozen copy of LLD Section 4 as at this revision. Later additions need their own
# ALTER TYPE ... ADD VALUE migration, so this list must never be edited.
ENUMS: dict[str, tuple[str, ...]] = {
    "user_role": ("applicant", "admissions", "compliance", "finance", "senior", "admin"),
    "application_status": ("draft", "submitted", "academic_review", "info_requested", "offer_issued", "offer_accepted", "offer_declined", "compliance_review", "precas_ready", "cas_assigned", "rejected", "withdrawn"),
    "risk_rating": ("low", "medium", "high"),
    "document_category": ("passport", "qualification", "english", "statement", "financial", "enrolment_evidence", "report_evidence", "other"),
    "enrolment_status": ("awaiting_arrival", "enrolled", "deferred", "interrupted", "withdrawn", "completed", "non_enrolled"),
    "attendance_status": ("present", "absent", "authorised_absence"),
    "visa_outcome": ("granted", "refused", "withdrawn", "not_used"),
    "event_type": ("non_enrolment", "withdrawal", "deferral", "course_change", "interruption", "curtailment", "change_of_circumstances", "cas_withdrawal", "sponsor_details_change"),
    "payment_type": ("payment", "deposit", "scholarship", "refund"),
}


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # --- Section 4: enumerations as PostgreSQL types -------------------------------------
    for type_name, members in ENUMS.items():
        values = ", ".join(f"'{v}'" for v in members)
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({values})")

    # --- updated_at trigger function (LLD 1.1) --------------------------------------------
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # --- 3.1 users ----------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE users (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email            citext      NOT NULL,
            password_hash    text        NOT NULL,
            full_name        text        NOT NULL,
            role             user_role   NOT NULL,
            status           text        NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'suspended', 'deactivated')),
            mfa_secret       text        NULL,
            failed_attempts  int         NOT NULL DEFAULT 0,
            locked_until     timestamptz NULL,
            last_sign_in_at  timestamptz NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ix_users_email ON users (email)")
    op.execute("CREATE INDEX ix_users_role_status ON users (role, status)")

    # --- 3.12 programmes and intakes ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE programmes (
            id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            title                text    NOT NULL,
            qualification_level  text    NOT NULL,
            duration_months      int     NOT NULL CHECK (duration_months > 0),
            atas_required        boolean NOT NULL DEFAULT false,
            status               text    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE intakes (
            id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            label                 text NOT NULL,
            start_date            date NOT NULL,
            application_deadline  date NOT NULL,
            status                text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            CHECK (application_deadline <= start_date)
        )
        """
    )

    # --- 3.11 agents (document FKs added after documents exists) ----------------------------
    op.execute(
        """
        CREATE TABLE agents (
            id                          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name                        text NOT NULL,
            country                     text NOT NULL,
            contact_name                text NULL,
            contact_email               text NULL,
            contact_phone               text NULL,
            agreement_document_id       bigint NULL,
            due_diligence_document_id   bigint NULL,
            due_diligence_date          date NULL,
            next_review_date            date NULL,
            status                      text NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active', 'under_review', 'suspended', 'deactivated')),
            refusal_rate_threshold_pct  numeric(4,1) NULL,
            created_at                  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # --- 3.2 applications --------------------------------------------------------------------
    op.execute("CREATE SEQUENCE application_ref_seq START 127")  # LLD 8.1
    op.execute(
        """
        CREATE TABLE applications (
            id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ref               text               NOT NULL,
            applicant_id      bigint             NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            agent_id          bigint             NULL REFERENCES agents(id) ON DELETE RESTRICT,
            owner_id          bigint             NULL REFERENCES users(id) ON DELETE RESTRICT,
            status            application_status NOT NULL DEFAULT 'draft',
            programme_id      bigint             NULL REFERENCES programmes(id) ON DELETE RESTRICT,
            intake_id         bigint             NULL REFERENCES intakes(id) ON DELETE RESTRICT,
            data              jsonb              NOT NULL DEFAULT '{}'::jsonb,
            withdrawn_reason  text               NULL,
            created_at        timestamptz        NOT NULL DEFAULT now(),
            updated_at        timestamptz        NOT NULL DEFAULT now(),
            CHECK (ref ~ '^ESE-[0-9]{4}-[0-9]{6}$')
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ix_applications_ref ON applications (ref)")
    op.execute("CREATE INDEX ix_applications_status ON applications (status)")
    op.execute("CREATE INDEX ix_applications_applicant ON applications (applicant_id)")
    op.execute("CREATE INDEX ix_applications_intake_status ON applications (intake_id, status)")
    # Duplicate detection support (FR-15)
    op.execute("CREATE INDEX ix_applications_passport ON applications ((data->>'passport_number'))")
    op.execute(
        "CREATE INDEX ix_applications_dob_name ON applications "
        "((data->>'date_of_birth'), lower(data->>'full_name'))"
    )
    op.execute("CREATE INDEX ix_applications_email ON applications (lower(data->>'email'))")
    op.execute(
        "CREATE TRIGGER trg_applications_updated_at BEFORE UPDATE ON applications "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # --- 3.3 documents -----------------------------------------------------------------------
    # student_id FK to student_records(id) is added when that table is created (Sprint 4).
    op.execute(
        """
        CREATE TABLE documents (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            application_id  bigint            NULL REFERENCES applications(id) ON DELETE RESTRICT,
            student_id      bigint            NULL,
            category        document_category NOT NULL,
            filename        text              NOT NULL,
            storage_key     text              NOT NULL,
            content_type    text              NOT NULL
                            CHECK (content_type IN ('application/pdf', 'image/jpeg', 'image/png')),
            size_bytes      int               NOT NULL,
            scan_status     text              NOT NULL DEFAULT 'pending'
                            CHECK (scan_status IN ('pending', 'clean', 'quarantined')),
            verified_by     bigint            NULL REFERENCES users(id) ON DELETE RESTRICT,
            verified_at     timestamptz       NULL,
            expiry_date     date              NULL,
            superseded_by   bigint            NULL REFERENCES documents(id) ON DELETE RESTRICT,
            uploaded_by     bigint            NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            uploaded_at     timestamptz       NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT ck_documents_size CHECK (size_bytes > 0 AND size_bytes <= 5242880),
            CONSTRAINT ck_documents_owner CHECK (application_id IS NOT NULL OR student_id IS NOT NULL)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ix_documents_storage_key ON documents (storage_key)")
    op.execute("CREATE INDEX ix_documents_application ON documents (application_id, category)")
    op.execute(
        "ALTER TABLE agents "
        "ADD CONSTRAINT fk_agents_agreement_document FOREIGN KEY (agreement_document_id) "
        "REFERENCES documents(id) ON DELETE RESTRICT, "
        "ADD CONSTRAINT fk_agents_due_diligence_document FOREIGN KEY (due_diligence_document_id) "
        "REFERENCES documents(id) ON DELETE RESTRICT"
    )

    # --- 3.10 audit_entries ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE audit_entries (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id         bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            user_name       text        NOT NULL,
            user_role       text        NOT NULL,
            action          text        NOT NULL CHECK (action ~ '^[a-z_]+\\.[a-z_]+$'),
            application_id  bigint      NULL REFERENCES applications(id) ON DELETE RESTRICT,
            student_id      bigint      NULL,
            entity_type     text        NULL,
            entity_id       bigint      NULL,
            detail          text        NOT NULL DEFAULT '',
            ip_address      inet        NULL,
            at              timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_application ON audit_entries (application_id, at DESC)")
    op.execute("CREATE INDEX ix_audit_student ON audit_entries (student_id, at DESC)")
    op.execute("CREATE INDEX ix_audit_user_at ON audit_entries (user_id, at DESC)")

    # --- Grants: tamper-evidence enforced by grant, not by application code (NFR-12) ---------
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') THEN
                CREATE ROLE app_role NOLOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO app_role")
    op.execute("GRANT SELECT, INSERT, UPDATE ON users, programmes, intakes, agents, applications, documents TO app_role")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_entries FROM app_role")
    op.execute("GRANT INSERT, SELECT ON audit_entries TO app_role")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE application_ref_seq TO app_role")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
