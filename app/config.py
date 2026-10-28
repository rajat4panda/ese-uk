"""Environment-driven settings (LLD Section 17 and 19).

Deployment values (connection strings, secrets, adapters) come from the environment.
Compliance thresholds that ESE may change at run time live in the system_settings table
and are read through services/settings.py, so that every change is audited.
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "replace-with-32-byte-random-secret"  # noqa: S105 — the value we refuse


class Settings(BaseSettings):
    # ESE_ENV_FILE="" turns the .env file off; the test suite does that, so a developer's local .env
    # cannot change what the tests see.
    model_config = SettingsConfigDict(env_file=os.environ.get("ESE_ENV_FILE", ".env") or None,
                                      env_file_encoding="utf-8", extra="ignore")

    ENVIRONMENT: Literal["development", "uat", "test", "production"] = "development"

    DATABASE_URL: str = "postgresql+psycopg://ese_app:ese_app@localhost:5432/ese_dev"
    MIGRATION_DATABASE_URL: str | None = None

    SECRET_KEY: str = PLACEHOLDER_SECRET
    JWT_ALGORITHM: str = "HS256"
    SESSION_EXPIRY_HOURS: int = 12  # NFR-06
    SESSION_INACTIVITY_MINUTES: int = 30  # NFR-06 inactivity timeout
    PASSWORD_RESET_MINUTES: int = 60  # LLD 3.13
    ACTIVATION_TOKEN_HOURS: int = 72
    EMAIL_VERIFICATION_HOURS: int = 48

    REDIS_URL: str = "redis://localhost:6379/0"

    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10  # LLD 16 — per IP on auth endpoints
    RATE_LIMIT_GENERAL_PER_MINUTE: int = 300  # LLD 16 — per token

    STORAGE_BACKEND: Literal["local", "s3", "memory"] = "local"
    STORAGE_LOCAL_PATH: str = "var/storage"
    STORAGE_BUCKET: str = "ese-docs-dev"
    STORAGE_REGION: str = "eu-west-2"
    S3_ENDPOINT_URL: str | None = None

    ANTIVIRUS_BACKEND: Literal["clamav", "simulated"] = "simulated"
    ANTIVIRUS_ENDPOINT: str | None = None  # host:port of clamd
    ANTIVIRUS_TIMEOUT_SECONDS: float = 30.0

    MAX_UPLOAD_BYTES: int = Field(default=5_242_880, le=5_242_880)  # FR-17; DB CHECK enforces the ceiling

    MAIL_BACKEND: Literal["console", "smtp", "memory"] = "console"
    MAIL_FROM: str = "admissions-noreply@example.ac.uk"
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_STARTTLS: bool = True

    # Hotfix: email is a production concern. In development and UAT no email leaves the system
    # (each message is written to the log instead) and applicants do not have to confirm their
    # address before submitting. Leave both unset to follow ENVIRONMENT; set true or false to
    # override, e.g. EMAIL_VERIFICATION_REQUIRED=true to exercise the confirmation flow in UAT.
    # Production refuses to start with either turned off.
    EMAIL_ENABLED: bool | None = None
    EMAIL_VERIFICATION_REQUIRED: bool | None = None

    PUBLIC_BASE_URL: str = "http://localhost:5173"
    SERVE_WEB_CLIENT: bool = True  # serve web/dist from the API process when present
    SENTRY_DSN: str | None = None

    @property
    def migration_url(self) -> str:
        return self.MIGRATION_DATABASE_URL or self.DATABASE_URL

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def _email_default(self) -> bool:
        # Off only in the non-production environments people click through. The automated test
        # environment keeps both on, so the verification and notification tests still run.
        return self.ENVIRONMENT not in ("development", "uat")

    @property
    def email_enabled(self) -> bool:
        return self._email_default if self.EMAIL_ENABLED is None else self.EMAIL_ENABLED

    @property
    def email_verification_required(self) -> bool:
        if self.EMAIL_VERIFICATION_REQUIRED is None:
            return self._email_default
        return self.EMAIL_VERIFICATION_REQUIRED

    @model_validator(mode="after")
    def _refuse_default_secret(self) -> "Settings":
        # LLD 19: "SECRET_KEY must be set — application refuses to start on the default".
        if self.ENVIRONMENT != "test" and (self.SECRET_KEY == PLACEHOLDER_SECRET or len(self.SECRET_KEY) < 32):
            raise ValueError(
                "SECRET_KEY is unset, the placeholder, or shorter than 32 characters. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if self.is_production:
            if self.STORAGE_BACKEND != "s3":
                raise ValueError("Production requires STORAGE_BACKEND=s3 (encrypted object storage, NFR-04/08).")
            if self.ANTIVIRUS_BACKEND != "clamav":
                raise ValueError("Production requires ANTIVIRUS_BACKEND=clamav (FR-22).")
            if self.MAIL_BACKEND != "smtp":
                raise ValueError("Production requires MAIL_BACKEND=smtp.")
            if not self.email_enabled or not self.email_verification_required:
                raise ValueError("Production requires EMAIL_ENABLED and EMAIL_VERIFICATION_REQUIRED to be on.")
            if not self.MIGRATION_DATABASE_URL or self.MIGRATION_DATABASE_URL == self.DATABASE_URL:
                # If the app login owned the tables, the audit REVOKE would not bind it (NFR-12).
                raise ValueError(
                    "Production requires MIGRATION_DATABASE_URL (schema owner), distinct from DATABASE_URL."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
