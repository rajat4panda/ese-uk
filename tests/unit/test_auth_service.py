"""Password hashing, policy and tokens (LLD 16, NFR-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.errors import BadRequest, Unauthenticated
from app.services import auth


def test_bcrypt_cost_12_and_verify():
    h = auth.hash_password("Correct-Horse-Battery-9")
    assert h.startswith("$2b$12$")
    assert auth.verify_password("Correct-Horse-Battery-9", h)
    assert not auth.verify_password("correct-horse-battery-9", h)


def test_no_plaintext_path():
    h = auth.hash_password("Visible-Secret-Value-1")
    assert "Visible-Secret-Value-1" not in h


def test_input_truncated_to_72_bytes():
    base = "a" * 72
    h = auth.hash_password(base + "tail-one")
    assert auth.verify_password(base + "different-tail", h)  # bcrypt semantics, made explicit (LLD 16)


def test_verify_handles_missing_or_corrupt_hash():
    assert auth.verify_password("x", None) is False
    assert auth.verify_password("x", "not-a-bcrypt-hash") is False


@pytest.mark.parametrize(
    "pw,email",
    [("short", None), ("qwertyuiop", None), ("PASSWORD1234", None), ("abababababab", None),
     ("priya.raman-2026!", "priya.raman@example.com")],
)  # fmt: skip
def test_policy_rejects(pw, email):
    with pytest.raises(BadRequest) as exc:
        auth.check_password_policy(pw, email)
    assert exc.value.code == "ERR_WEAK_PASSWORD"


def test_policy_accepts_reasonable_passphrase():
    auth.check_password_policy("Tea-Kettle-Orbit-42", "priya@example.com")


def test_token_round_trip():
    token, claims = auth.issue_token(7, "senior", "Kristin")
    decoded = auth.decode_token(token)
    assert decoded.user_id == 7 and decoded.role == "senior" and decoded.jti == claims.jti
    assert 11 * 3600 < decoded.seconds_remaining <= 12 * 3600


def test_token_expired_and_tampered():
    old, _ = auth.issue_token(1, "applicant", "x", now=datetime.now(UTC) - timedelta(hours=13))
    with pytest.raises(Unauthenticated) as exc:
        auth.decode_token(old)
    assert exc.value.code == "ERR_TOKEN_EXPIRED"
    token, _ = auth.issue_token(1, "applicant", "x")
    head, body, sig = token.split(".")
    with pytest.raises(Unauthenticated):
        auth.decode_token(f"{head}.{body}.{sig[::-1]}")


def test_token_algorithm_none_rejected():
    import jwt

    forged = jwt.encode({"sub": "1", "role": "admin", "iat": 0, "exp": 9999999999, "jti": "x"}, key=None,
                        algorithm="none")  # fmt: skip
    with pytest.raises(Unauthenticated):
        auth.decode_token(forged)


def test_link_tokens_hashed():
    plain, digest = auth.new_link_token()
    assert len(plain) >= 40 and digest == auth.hash_link_token(plain) and plain not in digest


def test_secret_key_guard(monkeypatch):
    from app.config import PLACEHOLDER_SECRET, Settings

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(ENVIRONMENT="development", SECRET_KEY=PLACEHOLDER_SECRET)
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(ENVIRONMENT="production", SECRET_KEY="too-short")
    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        Settings(ENVIRONMENT="production", SECRET_KEY="k" * 40, STORAGE_BACKEND="local")
    prod = {"ENVIRONMENT": "production", "SECRET_KEY": "k" * 40, "STORAGE_BACKEND": "s3",
            "ANTIVIRUS_BACKEND": "clamav", "MAIL_BACKEND": "smtp", "DATABASE_URL": "postgresql+psycopg://app@db/x"}
    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL"):
        Settings(**prod, MIGRATION_DATABASE_URL="postgresql+psycopg://app@db/x")
    ok = Settings(**prod, MIGRATION_DATABASE_URL="postgresql+psycopg://owner@db/x")
    assert ok.is_production
