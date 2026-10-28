"""Seeded accounts must be accounts the system itself accepts.

The demo and bootstrap accounts are written straight to the ORM, which is the one path into the
users table that does not pass through the schema validation every typed address goes through. That
gap shipped a demo applicant on a `.test` address: the account could sign in, and then the
application form refused to save because its own email field failed validation. These tests close
the gap from both ends — the addresses the seeder produces, and the behaviour an applicant sees.
"""

from __future__ import annotations

import pytest

from pydantic_core import PydanticCustomError

from app.cli import DEMO_DOMAIN, LEGACY_DEMO_DOMAIN
from app.models.enums import UserRole
from app.schemas.common import normalise_email
from tests.fixtures import API

DEMO_LOCAL_PARTS = [
    "applicant", "admissions", "admissions2", "compliance", "compliance2", "finance",
    "senior", "admin",
]  # fmt: skip


@pytest.mark.parametrize("local", DEMO_LOCAL_PARTS)
def test_every_seeded_demo_address_passes_the_api_validator(local):
    """The exact check the API applies to a typed address, applied to what the seeder writes."""
    address = f"{local}@{DEMO_DOMAIN}"
    assert normalise_email(address) == address


def test_the_old_demo_domain_would_still_be_rejected():
    """Pins the reason the domain changed, so nobody moves it back.

    `.test` is a special-use name (RFC 2606/6761) and the validator refuses it. That refusal is
    correct for a live system — an applicant typing `me@somewhere.test` should be turned away — so
    the fix belongs in the seed data, not in the validator.
    """
    with pytest.raises(PydanticCustomError):
        normalise_email(f"applicant@{LEGACY_DEMO_DOMAIN}")


@pytest.mark.parametrize("role", list(UserRole))
def test_role_derived_demo_addresses_are_valid(role):
    """seed-demo builds most addresses from the role name, so a new role must not break this."""
    address = f"{role.value}@{DEMO_DOMAIN}"
    assert normalise_email(address) == address


def test_an_applicant_can_save_their_own_account_address_on_the_form(client, applicant):
    """The failure as a person met it: signed in, filled the form, refused on submit.

    Section 1's email field is the address the account was created with, so any account the system
    can create must also be an address the form will take.
    """
    created = client.post(f"{API}/applications", headers=applicant.headers)
    assert created.status_code in (200, 201)
    app_id = created.json()["id"]

    res = client.put(
        f"{API}/applications/{app_id}",
        headers=applicant.headers,
        json={"data": {"full_name": "Demo Applicant", "email": applicant.email}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["email"] == applicant.email.lower()


def test_a_draft_follows_its_account_when_the_address_changes(client, applicant, owner_engine):
    """The stale-snapshot trap, which the account rename alone did not close.

    `data["email"]` is copied from the account when the draft is created, and Section 1 shows that
    copy read-only. Change the account address afterwards — as the move off the .test domain did —
    and the old one stays on screen, while submission refuses the mismatch. The applicant cannot
    edit the field, so the application is stuck. The account is the authority: any save must bring
    the stored copy back into line.
    """
    from sqlalchemy import text

    created = client.post(f"{API}/applications", headers=applicant.headers)
    app_id = created.json()["id"]
    client.put(f"{API}/applications/{app_id}", headers=applicant.headers,
               json={"data": {"full_name": "Priya Raman"}}).raise_for_status()  # fmt: skip

    moved = applicant.email.replace("@", "+moved@")
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE users SET email = :new WHERE id = :uid"),
                     {"new": moved, "uid": applicant.id})  # fmt: skip

    res = client.put(f"{API}/applications/{app_id}", headers=applicant.headers,
                     json={"data": {"mobile": "+447700900123"}})  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["data"]["email"] == moved, "the saved copy still holds the old address"


def test_a_save_cannot_smuggle_in_a_different_address(client, applicant):
    """Re-asserting the account address must not become a way to set any address you like."""
    created = client.post(f"{API}/applications", headers=applicant.headers)
    app_id = created.json()["id"]

    res = client.put(f"{API}/applications/{app_id}", headers=applicant.headers,
                     json={"data": {"email": "someone.else@example.com"}})  # fmt: skip
    assert res.status_code == 422, res.text
    codes = [d["code"] for d in res.json()["error"]["details"]]
    assert "ERR_EMAIL_MISMATCH" in codes
