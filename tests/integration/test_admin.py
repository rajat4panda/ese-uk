"""User administration (LLD 5.6; FR-121, FR-132; T-26)."""

from __future__ import annotations

import pytest

from app.services import notifications
from tests.fixtures import API, audit_actions, last_link_token, login


def test_admin_creates_staff_with_activation_link(client, staff, owner_engine):
    admin = staff("admin")
    r = client.post(f"{API}/admin/users", headers=admin.headers,
                    json={"email": "lavinia@example.ac.uk", "full_name": "Lavinia Example", "role": "compliance"})
    assert r.status_code == 201, r.text
    user = r.json()
    assert user["role"] == "compliance" and user["status"] == "active" and user["last_sign_in_at"] is None
    msg = [m for m in notifications.OUTBOX if m.template_key == "staff.account_created"][0]
    assert msg.recipient == "lavinia@example.ac.uk" and "activate=1" in msg.body
    # Cannot sign in before activation; activation sets the password
    token = last_link_token("staff.account_created")
    ok = client.post(f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "Staff-Passphrase-2026"})
    assert ok.status_code == 200
    s = login(client, "lavinia@example.ac.uk", "Staff-Passphrase-2026")
    assert client.get(f"{API}/me", headers=s.headers).json()["role"] == "compliance"
    assert "admin.user_create" in audit_actions(owner_engine, user_id=admin.id)


def test_admin_cannot_create_applicant_or_duplicate(client, staff):
    admin = staff("admin")
    r = client.post(f"{API}/admin/users", headers=admin.headers,
                    json={"email": "a@example.ac.uk", "full_name": "A", "role": "applicant"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_INVALID_ROLE"
    staff("senior", "existing@example.ac.uk")
    r = client.post(f"{API}/admin/users", headers=admin.headers,
                    json={"email": "Existing@example.ac.uk", "full_name": "Dup", "role": "senior"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ERR_EMAIL_EXISTS"


@pytest.mark.parametrize("role", ["admissions", "compliance", "finance", "senior"])
def test_only_admin_manages_users(client, staff, role):
    s = staff(role)
    assert client.get(f"{API}/admin/users", headers=s.headers).status_code == 403
    assert client.post(f"{API}/admin/users", headers=s.headers,
                       json={"email": "x@example.ac.uk", "full_name": "X", "role": "admin"}).status_code == 403


@pytest.mark.priority("T-26")
def test_t26_role_change_revokes_tokens(client, staff, owner_engine):
    """T-26: admin changes a user's role -> active tokens revoked; admin.user_role_change audited;
    next request re-authenticates."""
    admin = staff("admin")
    target = staff("admissions")
    assert client.get(f"{API}/me", headers=target.headers).status_code == 200
    r = client.patch(f"{API}/admin/users/{target.id}", headers=admin.headers, json={"role": "compliance"})
    assert r.status_code == 200 and r.json()["role"] == "compliance"
    stale = client.get(f"{API}/me", headers=target.headers)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "ERR_TOKEN_EXPIRED"
    fresh = login(client, target.email)
    assert client.get(f"{API}/me", headers=fresh.headers).json()["role"] == "compliance"
    assert "admin.user_role_change" in audit_actions(owner_engine, user_id=admin.id)


def test_deactivate_and_reactivate(client, staff, owner_engine):
    admin = staff("admin")
    target = staff("senior")
    r = client.patch(f"{API}/admin/users/{target.id}", headers=admin.headers, json={"status": "deactivated"})
    assert r.json()["status"] == "deactivated"
    assert client.get(f"{API}/me", headers=target.headers).status_code == 401
    assert client.post(f"{API}/auth/login", json={"email": target.email, "password": "Correct-Horse-Battery-9"}).status_code == 401
    client.patch(f"{API}/admin/users/{target.id}", headers=admin.headers, json={"status": "active"})
    login(client, target.email)
    actions = audit_actions(owner_engine, user_id=admin.id)
    assert "admin.user_deactivate" in actions and "admin.user_reactivate" in actions


def test_admin_cannot_modify_self(client, staff):
    admin = staff("admin")
    r = client.patch(f"{API}/admin/users/{admin.id}", headers=admin.headers, json={"role": "senior"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_SELF_MODIFICATION"


def test_access_review_lists_staff_only(client, staff, applicant):
    admin = staff("admin")
    staff("finance")
    rows = client.get(f"{API}/admin/users/access-review", headers=admin.headers).json()
    assert {r["role"] for r in rows} == {"admin", "finance"}
    assert "last_sign_in_at" in rows[0]
    everyone = client.get(f"{API}/admin/users?include_applicants=true", headers=admin.headers).json()
    assert "applicant" in {r["role"] for r in everyone}
