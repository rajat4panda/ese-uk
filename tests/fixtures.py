"""Shared test helpers and fixtures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services import notifications
from app.services.auth import hash_password

API = "/api/v1"
PASSWORD = "Correct-Horse-Battery-9"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@dataclass
class Actor:
    id: int
    email: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def last_link_token(kind: str) -> str:
    for msg in reversed(notifications.OUTBOX):
        if msg.template_key == kind:
            m = re.search(r"token=([A-Za-z0-9_\-]+)", msg.body)
            assert m, msg.body
            return m.group(1)
    raise AssertionError(f"no {kind} message in outbox: {[m.template_key for m in notifications.OUTBOX]}")


def register(client: TestClient, email: str, name: str = "Priya Raman", verify: bool = True) -> Actor:
    r = client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD, "full_name": name})
    assert r.status_code == 201, r.text
    body = r.json()
    if verify:
        token = last_link_token("auth.verify_email")
        v = client.post(f"{API}/auth/verify-email/confirm", json={"token": token})
        assert v.status_code == 200, v.text
    return Actor(body["user"]["id"], email, body["token"])


def login(client: TestClient, email: str, password: str = PASSWORD) -> Actor:
    r = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    b = r.json()
    return Actor(b["user"]["id"], email, b["token"])


def complete_data(ref: dict, full_name: str = "Priya Raman", email: str = "priya@example.com",
                  agent_id: int | None = None) -> dict:  # fmt: skip
    """A payload that satisfies every Appendix A submission rule."""
    start: date = ref["intake_start"]
    data = {
        "full_name": full_name,
        "date_of_birth": "2004-05-17",
        "sex": "female",
        "country_of_birth": "IN",
        "email": email,
        "mobile": "+919812345678",
        "nationality": "IN",
        "passport_number": "z1234567",
        "passport_issue_date": "2022-03-01",
        "passport_expiry_date": (start + timedelta(days=5 * 365)).isoformat(),
        "passport_issuing_country": "IN",
        "home_address": {"line1": "12 MG Road", "city": "Kochi", "region": "Kerala", "postcode": "682016",
                         "country": "IN"},
        "programme_id": ref["programme_id"],
        "intake_id": ref["intake_id"],
        "study_mode": "full_time",
        "how_heard": "ese_website",
        "qualifications": [{"institution": "St Teresa's College", "title": "Higher Secondary Certificate",
                            "level": "secondary_school", "country": "IN", "start_date": "2020-06-01",
                            "end_date": "2022-03-31", "grade": "91%"}],
        "highest_level": "secondary_school",
        "english_route": "slt_test",
        "test_provider": "IELTS Academic (UKVI)",
        "test_reference": "24IN012345RAMP001A",
        "test_date": (date.today() - timedelta(days=90)).isoformat(),
        "test_scores": {"overall": 6.5, "reading": 6.5, "writing": 6.0, "listening": 7.0, "speaking": 6.5},
        "been_to_uk": False,
        "previous_visa_refusal": False,
        "personal_statement": "I want to study business at ESE because " + "x" * 220,
        "study_purpose": "To build a career in international business management after graduating.",
        "funding_source": "family",
        "funder_details": "My parents will fund tuition and living costs from savings.",
        "declaration_truthful": True,
        "declaration_data_consent": True,
        "declaration_signature_name": full_name,
    }  # fmt: skip
    if agent_id is not None:
        data["agent_id"] = agent_id
    return data


def create_staff(owner_engine: Engine, role: str, email: str | None = None, name: str | None = None) -> tuple[int, str]:
    email = email or f"{role}@staff.test"
    with owner_engine.connect() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, password_hash, full_name, role, email_verified_at) "
                 "VALUES (:e, :p, :n, CAST(:r AS user_role), :v) RETURNING id"),
            {"e": email, "p": hash_password(PASSWORD), "n": name or f"Staff {role.title()}", "r": role,
             "v": datetime.now(UTC)},
        ).scalar_one()  # fmt: skip
    return uid, email


@pytest.fixture
def applicant(client: TestClient) -> Actor:
    return register(client, "priya@example.com")


@pytest.fixture
def staff(client: TestClient, owner_engine: Engine):
    """Factory: staff('senior') -> Actor signed in with that role."""

    def make(role: str, email: str | None = None) -> Actor:
        _, e = create_staff(owner_engine, role, email)
        return login(client, e)

    return make


@pytest.fixture
def draft(client: TestClient, applicant: Actor) -> dict:
    r = client.post(f"{API}/applications", headers=applicant.headers)
    assert r.status_code == 201, r.text
    return r.json()


def audit_actions(owner_engine: Engine, application_id: int | None = None, user_id: int | None = None) -> list[str]:
    sql = "SELECT action FROM audit_entries WHERE true"
    params: dict = {}
    if application_id is not None:
        sql += " AND application_id = :a"
        params["a"] = application_id
    if user_id is not None:
        sql += " AND user_id = :u"
        params["u"] = user_id
    with owner_engine.connect() as conn:
        return list(conn.execute(text(sql + " ORDER BY at, id"), params).scalars())


# --- Sprint 2 workflow helpers ----------------------------------------------------------------
ACADEMIC_CHECKS = {
    "qualifications_verified": True, "entry_requirements_met": True,
    "progression_appropriate": True, "english_evidence_verified": True,
}
COMPLIANCE_CHECKS = {
    "identity_passport_verified": True, "previous_uk_study_checked": True, "immigration_history_checked": True,
    "english_evidence_verified": True, "credibility_assessed": True, "financial_status_checked": True,
    "documents_complete": True,
}


def submit_application(client: TestClient, applicant: Actor, app_id: int, ref: dict,
                       agent_id: int | None = None) -> dict:  # fmt: skip
    me = client.get(f"{API}/me", headers=applicant.headers).json()
    data = complete_data(ref, full_name=me["name"], email=me["email"], agent_id=agent_id)
    client.put(f"{API}/applications/{app_id}", json={"data": data}, headers=applicant.headers)
    r = client.post(f"{API}/applications/{app_id}/submit", headers=applicant.headers)
    assert r.status_code == 200, r.text
    return r.json()


def do_academic(client: TestClient, actor: Actor, app_id: int, *, outcome="recommend_offer", complete=True,
                checklist=None, comments="Meets the entry requirements.",
                english_evidence="slt_test", english_standard="IELTS UKVI 6.0"):  # fmt: skip
    """FR-36: an assessor who ticks the English check records what they accepted, so the helper
    does too — a fixture that skips it would be testing a path no real assessment can take."""
    return client.post(f"{API}/applications/{app_id}/academic", headers=actor.headers, json={
        "checklist": ACADEMIC_CHECKS if checklist is None else checklist,
        "outcome": outcome, "comments": comments, "complete": complete,
        "english_evidence": english_evidence, "english_standard": english_standard,
    })  # fmt: skip


def do_offer(client: TestClient, actor: Actor, app_id: int, *, offer_type="conditional", conditions=None,
             expiry_date=None):  # fmt: skip
    body = {"offer_type": offer_type,
            "conditions": ["Pay the tuition fee deposit in full"] if conditions is None else conditions}
    if expiry_date:
        body["expiry_date"] = expiry_date
    return client.post(f"{API}/applications/{app_id}/offer", headers=actor.headers, json=body)


def do_compliance(client: TestClient, actor: Actor, app_id: int, *, risk="low", complete=True, checklist=None,
                  comments="All checks satisfied."):  # fmt: skip
    return client.post(f"{API}/applications/{app_id}/compliance", headers=actor.headers, json={
        "checklist": COMPLIANCE_CHECKS if checklist is None else checklist,
        "risk_rating": risk, "atas_required": False, "comments": comments, "complete": complete,
    })  # fmt: skip


def pay(client: TestClient, actor: Actor, app_id: int, amount: str = "3000.00", kind: str = "deposit"):
    return client.post(f"{API}/applications/{app_id}/payments", headers=actor.headers, json={
        "type": kind, "amount": amount, "payment_date": date.today().isoformat(), "method": "bank_transfer",
        "reference": "TEST-REF-1",
    })  # fmt: skip


def cas_body(ref: dict, **overrides) -> dict:
    start = ref["intake_start"]
    body = {
        "cas_number": "E4G0ABC12345",
        "course_title": "BSc Test Programme",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=3 * 365)).isoformat(),
        # FR-52: the figures as declared on the CAS, matching the seeded fee schedule.
        "tuition_fee": "15000.00",
        "amount_paid": "3000.00",
        "level1_user": "Lavinia Example (Level 1 User)",
        "notes": "",
    }
    body.update(overrides)
    return body


def assign_cas(client: TestClient, actor: Actor, app_id: int, ref: dict, **overrides):
    return client.post(f"{API}/applications/{app_id}/cas", headers=actor.headers, json=cas_body(ref, **overrides))


def run_to_precas(client: TestClient, applicant: Actor, app_id: int, ref: dict, admissions: Actor,
                  compliance: Actor, finance: Actor, *, conditions_met: bool = True, deposit: bool = True,
                  agent_id: int | None = None):  # fmt: skip
    """Drive an application from draft to Pre-CAS Ready, the state the CAS chain starts from."""
    submit_application(client, applicant, app_id, ref, agent_id=agent_id)
    assert do_academic(client, admissions, app_id).status_code == 200
    offer = do_offer(client, admissions, app_id).json()
    assert client.post(f"{API}/offers/{offer['id']}/issue", headers=admissions.headers,
                       json={"issue_method": "email"}).status_code == 200  # fmt: skip
    if conditions_met:
        for condition in offer["conditions"]:
            client.patch(f"{API}/offers/{offer['id']}/conditions/{condition['id']}", headers=admissions.headers,
                         json={"status": "met"})  # fmt: skip
    assert client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers,
                       json={"response": "accepted"}).status_code == 200  # fmt: skip
    if deposit:
        assert pay(client, finance, app_id).status_code == 201
    assert do_compliance(client, compliance, app_id).status_code == 200
    return offer


# --- Sprint 3 helpers: the post-CAS world ------------------------------------------------------
def student_for(client: TestClient, actor: Actor, app_id: int) -> dict:
    r = client.get(f"{API}/applications/{app_id}/student", headers=actor.headers)
    assert r.status_code == 200, r.text
    return r.json()


def enrol(client: TestClient, actor: Actor, student_id: int, **overrides):
    body = {"enrolment_date": date.today().isoformat()}
    body.update(overrides)
    return client.post(f"{API}/students/{student_id}/enrol", headers=actor.headers, json=body)


def attend(client: TestClient, actor: Actor, student_id: int, *, day: date | None = None,
           module: str = "BUS101", status: str = "present", reason: str | None = None):  # fmt: skip
    body = {"session_date": (day or date.today()).isoformat(), "module": module, "status": status}
    if reason:
        body["absence_reason"] = reason
    return client.post(f"{API}/students/{student_id}/attendance", headers=actor.headers, json=body)


def status_change(client: TestClient, actor: Actor, student_id: int, *, change_type: str = "deferral",
                  effective: date | None = None, reason: str = "Deferring to the next intake on request."):  # fmt: skip
    return client.post(f"{API}/students/{student_id}/status-change", headers=actor.headers, json={
        "type": change_type, "effective_date": (effective or date.today()).isoformat(), "reason": reason,
    })  # fmt: skip


def record_visa(client: TestClient, actor: Actor, app_id: int, *, outcome: str = "granted",
                decision_date: date | None = None, category: str | None = None):  # fmt: skip
    body = {"outcome": outcome, "decision_date": (decision_date or date.today()).isoformat()}
    if category:
        body["refusal_reason_category"] = category
    return client.post(f"{API}/applications/{app_id}/visa-outcome", headers=actor.headers, json=body)


def pdf_text(content: bytes) -> str:
    """The visible text of a generated PDF, so assertions can read it as a person would."""
    import io

    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
