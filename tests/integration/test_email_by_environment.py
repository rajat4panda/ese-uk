"""Hotfix: no email and no email confirmation in development and UAT; production unchanged."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import notifications
from tests.fixtures import API, PASSWORD, submit_application


@pytest.fixture
def uat(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "uat")
    monkeypatch.setenv("MAIL_BACKEND", "smtp")  # even if SMTP is configured, nothing must be sent
    get_settings.cache_clear()
    sent: list = []
    monkeypatch.setattr(notifications, "_send_smtp", lambda msg: sent.append(msg))
    yield sent
    get_settings.cache_clear()


@pytest.mark.parametrize("env", ["development", "uat"])
def test_email_off_outside_production(monkeypatch, env):
    monkeypatch.setenv("ENVIRONMENT", env)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.email_enabled is False
        assert s.email_verification_required is False
    finally:
        get_settings.cache_clear()


def test_uat_applicant_can_submit_without_confirming_email(client, reference, uat):
    r = client.post(f"{API}/auth/register",
                    json={"email": "uat.user@example.com", "password": PASSWORD, "full_name": "Uat User"})
    assert r.status_code == 201, r.text
    assert r.json()["user"]["email_verified"] is True  # no "Confirm your email address" banner
    headers = {"Authorization": f"Bearer {r.json()['token']}"}

    class A:
        pass

    applicant = A()
    applicant.headers = headers
    app_id = client.post(f"{API}/applications", headers=headers).json()["id"]
    submit_application(client, applicant, app_id, reference)
    assert uat == []  # nothing went to SMTP
    assert client.post(f"{API}/auth/verify-email/resend", headers=headers).status_code == 400
