"""Authentication operational tables and system settings.

Revision ID: 0002_auth_operational
Revises: 0001_baseline
Create Date: 2026-09-21

LLD 3.13 password_reset_tokens and 3.12 system_settings (seeded with Section 17 defaults).

Additions to LLD v1.1, proposed for LLD v1.2 and flagged for Finteam review:
  * users.email_verified_at and email_verification_tokens — Appendix A requires the applicant
    email to be verified before submission, but LLD 3.1 and 5.1 give it nowhere to live.
Forward-only (LLD 19.1).
"""

from alembic import op

revision = "0002_auth_operational"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# LLD Section 17 defaults. MINIMUM_DEPOSIT is per fee schedule and arrives with finance (Sprint 3).
SETTINGS = [
    ("CAS_ALLOCATION", "7", "int"),
    ("REPORTING_WORKING_DAYS", "10", "int"),
    ("ESCALATION_DAYS_BEFORE", "3", "int"),
    ("NON_ENROLMENT_GRACE_DAYS", "14", "int"),
    ("ATTENDANCE_THRESHOLD_PCT", "80", "int"),
    ("DEPOSIT_GATE_ENABLED", "true", "bool"),
    ("BCA_REFUSAL_MAX_PCT", "5.0", "decimal"),
    ("BCA_ENROLMENT_MIN_PCT", "95.0", "decimal"),
    ("BCA_COMPLETION_MIN_PCT", "85.0", "decimal"),
    ("BCA_AMBER_BAND_PCT", "2.0", "decimal"),
    ("PERMISSION_EXPIRY_ALERT_DAYS", "60", "int"),
    ("OFFER_EXPIRY_ALERT_DAYS", "7", "int"),
    ("SESSION_EXPIRY_HOURS", "12", "int"),
    ("MAX_UPLOAD_BYTES", "5242880", "int"),
    # FR-133: the privacy notice version recorded with the applicant's data consent.
    ("PRIVACY_NOTICE_VERSION", "draft-2026-09", "text"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE password_reset_tokens (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id       bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_hash    text        NOT NULL UNIQUE,
            expires_at    timestamptz NOT NULL,
            used_at       timestamptz NULL,
            requested_ip  inet        NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_password_reset_tokens_user ON password_reset_tokens (user_id)")

    op.execute("ALTER TABLE users ADD COLUMN email_verified_at timestamptz NULL")
    op.execute(
        """
        CREATE TABLE email_verification_tokens (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_hash  text        NOT NULL UNIQUE,
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_email_verification_tokens_user ON email_verification_tokens (user_id)")

    op.execute(
        """
        CREATE TABLE system_settings (
            key         text PRIMARY KEY,
            value       text        NOT NULL,
            value_type  text        NOT NULL CHECK (value_type IN ('int', 'decimal', 'bool', 'text')),
            updated_by  bigint      NULL REFERENCES users(id) ON DELETE RESTRICT,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    values = ",\n".join(f"('{k}', '{v}', '{t}')" for k, v, t in SETTINGS)
    op.execute(f"INSERT INTO system_settings (key, value, value_type) VALUES {values}")  # noqa: S608 — constants

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON password_reset_tokens, email_verification_tokens, system_settings "
        "TO app_role"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only (LLD 19.1).")
