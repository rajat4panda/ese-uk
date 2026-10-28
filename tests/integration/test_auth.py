"""Authentication (LLD 5.1, 16): registration, sign-in, lockout, reset, verification, sessions."""

from __future__ import annotations

import pytest

from app.services import cache, notifications
from tests.fixtures import API, PASSWORD, audit_actions, last_link_token, login, register


def test_register_creates_applicant_and_sends_verification(client, owner_engine):
    r = client.post(f"{API}/auth/register",
                    json={"email": "New.Person@Example.com", "password": PASSWORD, "full_name": "  New   Person "})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["role"] == "applicant"
    assert body["user"]["email"] == "new.person@example.com"
    assert body["user"]["name"] == "New Person"
    assert body["user"]["email_verified"] is False
    assert body["token"]
    assert [m.template_key for m in notifications.OUTBOX] == ["auth.verify_email"]
    assert audit_actions(owner_engine, user_id=body["user"]["id"]) == ["auth.register"]


def test_register_cannot_choose_role(client):
    r = client.post(f"{API}/auth/register",
                    json={"email": "x@example.com", "password": PASSWORD, "full_name": "X", "role": "admin"})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "ERR_UNKNOWN_FIELD"


def test_register_duplicate_email_is_case_insensitive(client):
    register(client, "dup@example.com", verify=False)
    r = client.post(f"{API}/auth/register", json={"email": "DUP@example.com", "password": PASSWORD, "full_name": "D"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_EMAIL_EXISTS"


@pytest.mark.parametrize("password", ["short1!", "password1234", "aaaaaaaaaaaa", "someone-long-enough"])
def test_weak_passwords_rejected(client, password):
    r = client.post(f"{API}/auth/register",
                    json={"email": "someone@example.com", "password": password, "full_name": "S"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_WEAK_PASSWORD"


def test_invalid_email_rejected(client):
    r = client.post(f"{API}/auth/register", json={"email": "not-an-email", "password": PASSWORD, "full_name": "S"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_INVALID_EMAIL"


def test_login_success_and_me(client, applicant, owner_engine):
    a = login(client, applicant.email)
    me = client.get(f"{API}/me", headers=a.headers)
    assert me.status_code == 200
    assert me.json()["email"] == applicant.email
    assert "auth.login" in audit_actions(owner_engine, user_id=applicant.id)


def test_login_bad_credentials_generic(client, applicant, owner_engine):
    r1 = client.post(f"{API}/auth/login", json={"email": applicant.email, "password": "wrong-password-1"})
    r2 = client.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-1"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["code"] == r2.json()["error"]["code"] == "ERR_BAD_CREDENTIALS"
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]
    assert audit_actions(owner_engine).count("auth.login_failed") == 2


@pytest.mark.priority("T-17")
def test_t17_six_failed_signins_lock_account(client, applicant, owner_engine):
    """T-17: six failed sign-ins -> 423 ERR_ACCOUNT_LOCKED; six auth.login_failed plus one auth.locked."""
    codes = []
    for _ in range(6):
        r = client.post(f"{API}/auth/login", json={"email": applicant.email, "password": "wrong-password-1"})
        codes.append(r.status_code)
    assert codes[:4] == [401, 401, 401, 401]
    assert codes[5] == 423
    last = client.post  # noqa: F841
    actions = audit_actions(owner_engine, user_id=applicant.id)
    assert actions.count("auth.login_failed") == 6
    assert actions.count("auth.locked") == 1
    # The correct password does not bypass the lock.
    r = client.post(f"{API}/auth/login", json={"email": applicant.email, "password": PASSWORD})
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "ERR_ACCOUNT_LOCKED"


def test_lock_expiry_then_success_resets_counter(client, applicant, owner_engine):
    from sqlalchemy import text

    for _ in range(5):
        client.post(f"{API}/auth/login", json={"email": applicant.email, "password": "wrong-password-1"})
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE users SET locked_until = now() - interval '1 minute' WHERE id = :i"),
                     {"i": applicant.id})
        conn.commit()
    login(client, applicant.email)
    with owner_engine.connect() as conn:
        n = conn.execute(text("SELECT failed_attempts FROM users WHERE id = :i"), {"i": applicant.id}).scalar_one()
    assert n == 0


def test_tenth_failure_locks_for_an_hour(client, applicant, owner_engine):
    from sqlalchemy import text

    for i in range(10):
        if i == 5:  # let the 15-minute lock lapse
            with owner_engine.connect() as conn:
                conn.execute(text("UPDATE users SET locked_until = now() - interval '1 second' WHERE id = :i"),
                             {"i": applicant.id})
                conn.commit()
        client.post(f"{API}/auth/login", json={"email": applicant.email, "password": "wrong-password-1"})
    with owner_engine.connect() as conn:
        mins = conn.execute(text("SELECT extract(epoch FROM locked_until - now()) / 60 FROM users WHERE id = :i"),
                            {"i": applicant.id}).scalar_one()
    assert 55 < float(mins) <= 60
    assert audit_actions(owner_engine, user_id=applicant.id).count("auth.locked") == 2


def test_password_reset_request_never_reveals_existence(client, applicant):
    r1 = client.post(f"{API}/auth/password-reset/request", json={"email": "nobody@example.com"})
    r2 = client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    assert [m.recipient for m in notifications.OUTBOX if m.template_key == "auth.password_reset"] == [applicant.email]


@pytest.mark.priority("T-27")
def test_t27_reset_link_single_use(client, applicant, owner_engine):
    """T-27: password reset link used twice -> second attempt 400 ERR_TOKEN_INVALID."""
    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    token = last_link_token("auth.password_reset")
    new_pw = "A-Brand-New-Passphrase-7"
    r1 = client.post(f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": new_pw})
    assert r1.status_code == 200
    r2 = client.post(f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "Another-Passphrase-8"})
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "ERR_TOKEN_INVALID"
    login(client, applicant.email, new_pw)
    assert "auth.password_reset" in audit_actions(owner_engine, user_id=applicant.id)


def test_reset_token_expired(client, applicant, owner_engine):
    from sqlalchemy import text

    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    token = last_link_token("auth.password_reset")
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute'"))
        conn.commit()
    r = client.post(f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "A-Brand-New-Pass-7"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_TOKEN_EXPIRED"


def test_reset_revokes_existing_sessions_and_clears_lock(client, applicant):
    for _ in range(5):
        client.post(f"{API}/auth/login", json={"email": applicant.email, "password": "wrong-password-1"})
    assert client.get(f"{API}/me", headers=applicant.headers).status_code == 200  # registration session still valid
    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    token = last_link_token("auth.password_reset")
    client.post(f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "A-Brand-New-Pass-7"})
    r = client.get(f"{API}/me", headers=applicant.headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR_TOKEN_EXPIRED"
    login(client, applicant.email, "A-Brand-New-Pass-7")


def test_reset_token_hash_only_stored(client, applicant, owner_engine):
    from sqlalchemy import text

    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    token = last_link_token("auth.password_reset")
    with owner_engine.connect() as conn:
        stored = conn.execute(text("SELECT token_hash FROM password_reset_tokens")).scalar_one()
    assert stored != token and len(stored) == 64


def test_email_verification_flow(client, owner_engine):
    a = register(client, "verify@example.com", verify=False)
    assert client.get(f"{API}/me", headers=a.headers).json()["email_verified"] is False
    token = last_link_token("auth.verify_email")
    assert client.post(f"{API}/auth/verify-email/confirm", json={"token": token}).status_code == 200
    assert client.get(f"{API}/me", headers=a.headers).json()["email_verified"] is True
    again = client.post(f"{API}/auth/verify-email/confirm", json={"token": token})
    assert again.json()["error"]["code"] == "ERR_TOKEN_INVALID"
    assert "auth.email_verified" in audit_actions(owner_engine, user_id=a.id)


def test_resend_verification(client):
    a = register(client, "resend@example.com", verify=False)
    r = client.post(f"{API}/auth/verify-email/resend", headers=a.headers)
    assert r.status_code == 202
    assert [m.template_key for m in notifications.OUTBOX].count("auth.verify_email") == 2


def test_logout_revokes_token(client, applicant, owner_engine):
    assert client.post(f"{API}/auth/logout", headers=applicant.headers).status_code == 204
    r = client.get(f"{API}/me", headers=applicant.headers)
    assert r.status_code == 401
    assert "auth.logout" in audit_actions(owner_engine, user_id=applicant.id)


def test_inactivity_timeout(client, applicant):
    from app.services.auth import decode_token

    jti = decode_token(applicant.token).jti
    cache.get_redis().delete(f"session:{jti}")  # simulate the idle TTL elapsing
    r = client.get(f"{API}/me", headers=applicant.headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR_TOKEN_EXPIRED"


def test_missing_and_garbage_tokens(client):
    assert client.get(f"{API}/me").json()["error"]["code"] == "ERR_UNAUTHENTICATED"
    r = client.get(f"{API}/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_expired_jwt(client, applicant):
    from datetime import UTC, datetime, timedelta

    from app.services.auth import issue_token

    token, _ = issue_token(applicant.id, "applicant", "x", now=datetime.now(UTC) - timedelta(hours=13))
    r = client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR_TOKEN_EXPIRED"


def test_deactivated_user_cannot_sign_in(client, applicant, owner_engine):
    from sqlalchemy import text

    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE users SET status='deactivated' WHERE id=:i"), {"i": applicant.id})
        conn.commit()
    r = client.post(f"{API}/auth/login", json={"email": applicant.email, "password": PASSWORD})
    assert r.status_code == 401
    assert client.get(f"{API}/me", headers=applicant.headers).status_code == 401


def test_unknown_account_lockout_mirrors_real_one(client):
    codes = [client.post(f"{API}/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-1"}).status_code
             for _ in range(6)]
    assert codes[:4] == [401] * 4 and codes[4:] == [423, 423]


def test_successful_reset_kills_other_outstanding_links(client, applicant):
    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    first = last_link_token("auth.password_reset")
    client.post(f"{API}/auth/password-reset/request", json={"email": applicant.email})
    second = last_link_token("auth.password_reset")
    assert client.post(f"{API}/auth/password-reset/confirm",
                       json={"token": second, "new_password": "A-Brand-New-Pass-7"}).status_code == 200
    r = client.post(f"{API}/auth/password-reset/confirm", json={"token": first, "new_password": "Attacker-Pass-99x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ERR_TOKEN_INVALID"
