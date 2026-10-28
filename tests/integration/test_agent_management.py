"""Maintaining the recruitment agent register (BRD FR-90 to FR-94).

What a compliance visit would test: that only the right people can change the register; that every
change, with its old value and its reason, is in the audit trail; that suspending an agent stops new
attribution without erasing the agent; that evidence is scanned before it is kept and superseded
rather than overwritten; and that a review coming due is chased once, not every morning.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.services.storage import get_storage
from app.workers.jobs.compliance import agent_review_reminder
from tests.fixtures import API, EICAR, PDF_BYTES, PNG_BYTES, complete_data


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior"), "admin": staff("admin")}  # fmt: skip


@pytest.fixture
def session(owner_engine: Engine) -> Session:
    from app.database import get_sessionmaker

    db = get_sessionmaker()()
    yield db
    db.close()


def agent_audit(owner_engine: Engine, agent_id: int) -> list[tuple[str, str]]:
    with owner_engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT action, detail FROM audit_entries WHERE entity_type = 'agent' AND entity_id = :a ORDER BY at, id"
        ), {"a": agent_id})]  # fmt: skip


def new_agent(client, actor, **extra) -> dict:
    body = {"name": "Global Pathways Ltd", "country": "gh", "contact_name": "Ama Mensah",
            "contact_email": "Ama@GlobalPathways.example", **extra}  # fmt: skip
    res = client.post(f"{API}/agents", json=body, headers=actor.headers)
    assert res.status_code == 201, res.text
    return res.json()


# --- FR-90: add and correct ---------------------------------------------------------------------
def test_compliance_adds_an_agent_and_it_is_audited(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"], contact_phone="+233 20 123 4567",
                      due_diligence_date="2026-01-15", next_review_date="2027-01-15",
                      refusal_rate_threshold_pct=10)  # fmt: skip
    assert agent["status"] == "active"
    assert agent["country_code"] == "GH" and agent["country"] == "Ghana"
    assert agent["contact_email"] == "ama@globalpathways.example"
    assert agent["refusal_rate_threshold_pct"] == 10.0
    assert agent["evidence"] == {"agreement": None, "due_diligence": None}
    assert agent_audit(owner_engine, agent["id"]) == [("agent.create", "Added agent Global Pathways Ltd (GH)")]

    listed = {r["name"]: r for r in client.get(f"{API}/agents", headers=team["admissions"].headers).json()}
    assert listed["Global Pathways Ltd"]["contact_name"] == "Ama Mensah"


def test_new_agent_can_be_attributed_straight_away(client, reference, team, applicant, draft):
    agent = new_agent(client, team["compliance"])
    data = complete_data(reference, full_name="Priya Raman", email="priya@example.com", agent_id=agent["id"])
    res = client.put(f"{API}/applications/{draft['id']}", json={"data": data}, headers=applicant.headers)
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("role", ["admissions", "finance"])
def test_only_compliance_senior_and_admin_change_the_register(client, reference, team, role):
    res = client.post(f"{API}/agents", json={"name": "Nope Ltd", "country": "IN"}, headers=team[role].headers)
    assert res.status_code == 403
    res = client.patch(f"{API}/agents/{reference['agent_id']}", json={"contact_name": "X"}, headers=team[role].headers)
    assert res.status_code == 403
    res = client.post(f"{API}/agents/{reference['agent_id']}/status",
                      json={"status": "suspended", "reason": "Because I can"}, headers=team[role].headers)  # fmt: skip
    assert res.status_code == 403


def test_senior_and_admin_can_change_the_register(client, reference, team):
    assert client.post(f"{API}/agents", json={"name": "Senior Adds Ltd", "country": "IN"},
                       headers=team["senior"].headers).status_code == 201  # fmt: skip
    assert client.post(f"{API}/agents", json={"name": "Admin Adds Ltd", "country": "IN"},
                       headers=team["admin"].headers).status_code == 201  # fmt: skip


def test_applicant_cannot_read_an_agent(client, reference, applicant):
    assert client.get(f"{API}/agents/{reference['agent_id']}", headers=applicant.headers).status_code == 403


def test_duplicate_name_in_the_same_country_is_refused(client, reference, team):
    new_agent(client, team["compliance"])
    res = client.post(f"{API}/agents", json={"name": "global pathways ltd", "country": "GH"},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 400 and res.json()["error"]["code"] == "ERR_AGENT_EXISTS"
    # The same trading name in another country is a different agent.
    assert client.post(f"{API}/agents", json={"name": "Global Pathways Ltd", "country": "NG"},
                       headers=team["compliance"].headers).status_code == 201  # fmt: skip


def test_invalid_input_is_refused_field_by_field(client, reference, team):
    res = client.post(f"{API}/agents", json={"name": "A", "country": "XX", "contact_email": "not-an-email",
                                             "next_review_date": "2020-01-01", "due_diligence_date": "2021-01-01"},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 422
    fields = {d["field"] for d in res.json()["error"]["details"]}
    assert {"name", "country", "contact_email", "next_review_date"} <= fields


def test_edit_records_old_and_new_values(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    res = client.patch(f"{API}/agents/{agent['id']}",
                       json={"contact_name": "Kofi Boateng", "next_review_date": "2027-03-01"},
                       headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["contact_name"] == "Kofi Boateng"
    action, detail = agent_audit(owner_engine, agent["id"])[-1]
    assert action == "agent.update"
    assert "contact_name: Ama Mensah -> Kofi Boateng" in detail
    assert "next_review_date: — -> 2027-03-01" in detail


def test_an_edit_that_changes_nothing_writes_no_audit(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    client.patch(f"{API}/agents/{agent['id']}", json={"contact_name": "Ama Mensah"}, headers=team["compliance"].headers)
    assert [a for a, _ in agent_audit(owner_engine, agent["id"])] == ["agent.create"]


def test_edit_checks_review_date_against_the_stored_due_diligence_date(client, reference, team):
    agent = new_agent(client, team["compliance"], due_diligence_date="2026-06-01")
    res = client.patch(f"{API}/agents/{agent['id']}", json={"next_review_date": "2026-05-01"},
                       headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 422  # same status as when both dates are sent together
    assert res.json()["error"]["details"][0]["field"] == "next_review_date"


def test_name_cannot_be_cleared(client, reference, team):
    agent = new_agent(client, team["compliance"])
    res = client.patch(f"{API}/agents/{agent['id']}", json={"name": None}, headers=team["compliance"].headers)
    assert res.status_code == 400


def test_status_is_not_changed_by_an_edit(client, reference, team):
    agent = new_agent(client, team["compliance"])
    res = client.patch(f"{API}/agents/{agent['id']}", json={"status": "suspended"}, headers=team["compliance"].headers)
    assert res.status_code == 422  # unknown field: status has its own endpoint, with a reason


# --- FR-94: suspend, deactivate, reactivate -----------------------------------------------------------
def test_suspending_needs_a_reason(client, reference, team):
    agent = new_agent(client, team["compliance"])
    res = client.post(f"{API}/agents/{agent['id']}/status", json={"status": "suspended"},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == "reason"


def test_suspended_agent_stays_listed_and_cannot_be_attributed(
    client, reference, team, applicant, draft, owner_engine
):
    agent = new_agent(client, team["compliance"])
    res = client.post(f"{API}/agents/{agent['id']}/status",
                      json={"status": "suspended", "reason": "Refusal rate above threshold pending review"},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "suspended"
    assert res.json()["status_reason"] == "Refusal rate above threshold pending review"

    listed = {r["name"]: r for r in client.get(f"{API}/agents", headers=team["admissions"].headers).json()}
    assert listed["Global Pathways Ltd"]["status"] == "suspended"
    assert listed["Global Pathways Ltd"]["status_reason"].startswith("Refusal rate")

    data = complete_data(reference, full_name="Priya Raman", email="priya@example.com", agent_id=agent["id"])
    res = client.put(f"{API}/applications/{draft['id']}", json={"data": data}, headers=applicant.headers)
    assert res.status_code in (400, 422)

    action, detail = agent_audit(owner_engine, agent["id"])[-1]
    assert action == "agent.status_change"
    assert "active -> suspended" in detail and "Refusal rate above threshold" in detail


def test_reactivating_clears_the_reason_and_is_audited(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    client.post(f"{API}/agents/{agent['id']}/status", json={"status": "deactivated", "reason": "Agreement ended 2026"},
                headers=team["compliance"].headers)  # fmt: skip
    res = client.post(f"{API}/agents/{agent['id']}/status", json={"status": "active"}, headers=team["senior"].headers)
    assert res.status_code == 200
    assert res.json()["status"] == "active" and res.json()["status_reason"] is None
    assert [a for a, _ in agent_audit(owner_engine, agent["id"])][-2:] == ["agent.status_change", "agent.status_change"]


def test_a_deactivated_agent_comes_back_only_by_reactivation(client, reference, team):
    agent = new_agent(client, team["compliance"])
    client.post(f"{API}/agents/{agent['id']}/status", json={"status": "deactivated", "reason": "Agreement ended 2026"},
                headers=team["compliance"].headers)  # fmt: skip
    res = client.post(f"{API}/agents/{agent['id']}/status", json={"status": "under_review", "reason": "Looking again at it"},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 409


def test_service_requires_a_reason_even_without_the_schema(client, reference, team, session):
    from app.errors import BadRequest
    from app.models.user import User
    from app.services import agents as svc

    agent = new_agent(client, team["compliance"])
    actor = session.get(User, team["compliance"].id)
    with pytest.raises(BadRequest):
        svc.set_status(session, actor, agent["id"], "suspended", "  short ", None)
    session.rollback()


def test_last_changed_moves_on_every_change(client, reference, team):
    agent = new_agent(client, team["compliance"])
    edited = client.patch(f"{API}/agents/{agent['id']}", json={"contact_name": "Kofi"}, headers=team["compliance"].headers).json()
    assert edited["updated_at"] > agent["updated_at"]
    suspended = client.post(f"{API}/agents/{agent['id']}/status", json={"status": "suspended", "reason": "Pending a check"},
                            headers=team["compliance"].headers).json()  # fmt: skip
    assert suspended["updated_at"] > edited["updated_at"]


def test_bad_threshold_is_refused_not_cleared(client, reference, team):
    agent = new_agent(client, team["compliance"], refusal_rate_threshold_pct=10)
    res = client.patch(f"{API}/agents/{agent['id']}", json={"refusal_rate_threshold_pct": "10%"},
                       headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 422
    assert client.get(f"{API}/agents/{agent['id']}", headers=team["compliance"].headers).json()["refusal_rate_threshold_pct"] == 10.0


def test_setting_the_same_status_again_is_refused(client, reference, team):
    agent = new_agent(client, team["compliance"])
    res = client.post(f"{API}/agents/{agent['id']}/status", json={"status": "active"}, headers=team["compliance"].headers)
    assert res.status_code == 409


# --- FR-91: evidence -------------------------------------------------------------------------------
def upload(client, actor, agent_id, kind, content=PDF_BYTES, name="agreement.pdf"):
    return client.post(f"{API}/agents/{agent_id}/evidence", data={"kind": kind},
                       files={"file": (name, content, "application/pdf")}, headers=actor.headers)  # fmt: skip


def test_agreement_is_uploaded_and_downloaded_with_audit(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    res = upload(client, team["compliance"], agent["id"], "agreement")
    assert res.status_code == 201, res.text
    ev = res.json()["evidence"]["agreement"]
    assert ev["filename"] == "agreement.pdf" and ev["available"] is True
    assert res.json()["evidence"]["due_diligence"] is None

    got = client.get(f"{API}/agents/{agent['id']}/evidence/agreement", headers=team["admissions"].headers)
    assert got.status_code == 200 and got.content == PDF_BYTES
    assert "no-store" in got.headers["cache-control"]
    with owner_engine.connect() as conn:
        actions = list(conn.execute(text(
            "SELECT action FROM audit_entries WHERE entity_type = 'document' AND entity_id = :d ORDER BY at, id"
        ), {"d": ev["id"]}).scalars())  # fmt: skip
    assert actions == ["document.upload", "document.view"]


def test_a_new_file_supersedes_the_old_one(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    first = upload(client, team["compliance"], agent["id"], "due_diligence", name="dd-2025.pdf").json()
    second = upload(client, team["compliance"], agent["id"], "due_diligence", PNG_BYTES, "dd-2026.png").json()
    old_id = first["evidence"]["due_diligence"]["id"]
    new_id = second["evidence"]["due_diligence"]["id"]
    assert new_id != old_id and second["evidence"]["due_diligence"]["filename"] == "dd-2026.png"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT superseded_by FROM documents WHERE id = :d"), {"d": old_id}).scalar() == new_id


def test_infected_evidence_is_refused_and_kept_as_evidence(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    res = upload(client, team["compliance"], agent["id"], "agreement", b"%PDF-1.4\n" + EICAR, "eicar.pdf")
    assert res.status_code == 422 and res.json()["error"]["code"] == "ERR_SCAN_FAILED"
    detail = client.get(f"{API}/agents/{agent['id']}", headers=team["compliance"].headers).json()
    assert detail["evidence"]["agreement"] is None  # the agent does not point at a quarantined file
    with owner_engine.connect() as conn:
        status, key = conn.execute(text(
            "SELECT scan_status, storage_key FROM documents WHERE agent_id = :a"), {"a": agent["id"]}).one()  # fmt: skip
    assert status == "quarantined"
    assert not get_storage().exists(key)
    with owner_engine.connect() as conn:
        actions = list(conn.execute(text(
            "SELECT action FROM audit_entries WHERE entity_type = 'document' ORDER BY at, id")).scalars())  # fmt: skip
    assert actions == ["document.upload", "document.quarantine"]


def test_wrong_file_type_and_wrong_kind_are_refused(client, reference, team):
    agent = new_agent(client, team["compliance"])
    assert upload(client, team["compliance"], agent["id"], "agreement", b"MZ\x90\x00", "tool.exe").status_code == 400
    assert upload(client, team["compliance"], agent["id"], "passport").status_code == 422


def test_admissions_reads_evidence_but_cannot_upload_it(client, reference, team):
    agent = new_agent(client, team["compliance"])
    assert upload(client, team["admissions"], agent["id"], "agreement").status_code == 403
    assert client.get(f"{API}/agents/{agent['id']}/evidence/agreement",
                      headers=team["admissions"].headers).status_code == 404  # nothing held yet  # fmt: skip
    assert client.get(f"{API}/agents/{agent['id']}/evidence/agreement",
                      headers=team["finance"].headers).status_code == 403  # fmt: skip


def test_agent_documents_do_not_appear_on_application_screens(client, reference, team, applicant, draft):
    """The application has a document of its own, so an empty list cannot pass by accident."""
    res = client.post(f"{API}/applications/{draft['id']}/documents", data={"category": "passport"},
                      files={"file": ("passport.pdf", PDF_BYTES, "application/pdf")}, headers=applicant.headers)  # fmt: skip
    assert res.status_code == 201, res.text
    agent = new_agent(client, team["compliance"])
    assert upload(client, team["compliance"], agent["id"], "agreement").status_code == 201
    record = client.get(f"{API}/applications/{draft['id']}", headers=team["compliance"].headers)
    assert [d["category"] for d in record.json()["documents"]] == ["passport"]


@pytest.mark.parametrize("category", ["agent_agreement", "agent_due_diligence"])
def test_agent_categories_cannot_be_filed_on_an_application(client, reference, team, draft, category):
    res = client.post(f"{API}/applications/{draft['id']}/documents", data={"category": category},
                      files={"file": ("x.pdf", PDF_BYTES, "application/pdf")}, headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 400


def test_evidence_route_refuses_an_oversized_body_before_reading_it(client, reference, team):
    """The body-size guards key on the upload paths; this route must be one of them."""
    agent = new_agent(client, team["compliance"])
    res = client.post(f"{API}/agents/{agent['id']}/evidence", content=b"x",
                      headers={**team["compliance"].headers, "Content-Length": str(50 * 1024 * 1024),
                               "Content-Type": "multipart/form-data; boundary=x"})  # fmt: skip
    assert res.status_code == 400 and res.json()["error"]["code"] == "ERR_FILE_SIZE"


def test_evidence_over_the_file_limit_is_refused(client, reference, team):
    agent = new_agent(client, team["compliance"])
    big = b"%PDF-1.4\n" + b"0" * (6 * 1024 * 1024)
    res = upload(client, team["compliance"], agent["id"], "agreement", big, "big.pdf")
    assert res.status_code == 400 and res.json()["error"]["code"] == "ERR_FILE_SIZE"


@pytest.mark.parametrize("role", ["finance"])
def test_finance_cannot_upload_evidence(client, reference, team, role):
    agent = new_agent(client, team["compliance"])
    assert upload(client, team[role], agent["id"], "agreement").status_code == 403


def test_applicant_cannot_upload_or_download_evidence(client, reference, team, applicant):
    agent = new_agent(client, team["compliance"])
    upload(client, team["compliance"], agent["id"], "agreement")
    assert upload(client, applicant, agent["id"], "agreement").status_code == 403
    assert client.get(f"{API}/agents/{agent['id']}/evidence/agreement", headers=applicant.headers).status_code == 403


def test_upload_is_on_the_agents_own_trail(client, reference, team, owner_engine):
    agent = new_agent(client, team["compliance"])
    upload(client, team["compliance"], agent["id"], "agreement", name="v1.pdf")
    upload(client, team["compliance"], agent["id"], "agreement", name="v2.pdf")
    trail = agent_audit(owner_engine, agent["id"])
    assert trail[-2][0] == "agent.update" and "agreement: — -> v1.pdf" in trail[-2][1]
    assert trail[-1][0] == "agent.update" and "agreement: v1.pdf -> v2.pdf" in trail[-1][1]


def test_superseded_evidence_stays_listed_and_downloadable(client, reference, team):
    agent = new_agent(client, team["compliance"])
    first = upload(client, team["compliance"], agent["id"], "agreement", name="v1.pdf").json()
    detail = upload(client, team["compliance"], agent["id"], "agreement", PNG_BYTES, "v2.png").json()
    history = {h["filename"]: h for h in detail["evidence_history"]}
    assert history["v1.pdf"]["status"] == "superseded" and history["v2.png"]["status"] == "current"
    old_id = first["evidence"]["agreement"]["id"]
    got = client.get(f"{API}/agents/{agent['id']}/documents/{old_id}", headers=team["admissions"].headers)
    assert got.status_code == 200 and got.content == PDF_BYTES


def test_another_agents_document_cannot_be_read_through_this_agent(client, reference, team):
    a = new_agent(client, team["compliance"])
    b = client.post(f"{API}/agents", json={"name": "Other Ltd", "country": "IN"}, headers=team["compliance"].headers).json()
    doc_id = upload(client, team["compliance"], a["id"], "agreement").json()["evidence"]["agreement"]["id"]
    assert client.get(f"{API}/agents/{b['id']}/documents/{doc_id}", headers=team["compliance"].headers).status_code == 404


# --- FR-91: review reminders -----------------------------------------------------------------------------
def _queued(owner_engine: Engine) -> list[str]:
    with owner_engine.connect() as conn:
        return list(conn.execute(text(
            "SELECT dedupe_key FROM notification_queue WHERE template_key = 'agent.review_due' ORDER BY id"
        )).scalars())  # fmt: skip


def test_review_reminder_fires_once_when_due_and_once_when_overdue(client, reference, team, session, owner_engine):
    soon = date.today() + timedelta(days=10)
    agent = new_agent(client, team["compliance"], next_review_date=soon.isoformat())
    assert agent_review_reminder(session) == 1
    session.commit()
    assert agent_review_reminder(session) == 0  # second run the same day: nothing new
    session.commit()
    # The date passes with no review recorded: one more, marked overdue.
    assert agent_review_reminder(session, today=soon + timedelta(days=1)) == 1
    session.commit()
    assert agent_review_reminder(session, today=soon + timedelta(days=2)) == 0
    keys = _queued(owner_engine)
    assert keys == [f"agent-review:{agent['id']}:{soon.isoformat()}:due",
                    f"agent-review:{agent['id']}:{soon.isoformat()}:overdue"]  # fmt: skip


def test_recording_the_review_starts_a_new_cycle(client, reference, team, session, owner_engine):
    soon = date.today() + timedelta(days=5)
    agent = new_agent(client, team["compliance"], next_review_date=soon.isoformat())
    agent_review_reminder(session)
    session.commit()
    client.patch(f"{API}/agents/{agent['id']}", json={"due_diligence_date": date.today().isoformat(),
                                                      "next_review_date": (soon + timedelta(days=365)).isoformat()},
                 headers=team["compliance"].headers)  # fmt: skip
    session.expire_all()
    assert agent_review_reminder(session) == 0  # next review is a year away
    # Eleven months on, the new date enters the notice window: a fresh alert, not a duplicate.
    assert agent_review_reminder(session, today=soon + timedelta(days=345)) == 1


def test_review_reminder_skips_suspended_agents_and_distant_reviews(client, reference, team, session):
    far = new_agent(client, team["compliance"], next_review_date=(date.today() + timedelta(days=200)).isoformat())
    near = client.post(f"{API}/agents", json={"name": "Near Ltd", "country": "IN",
                                             "next_review_date": (date.today() + timedelta(days=3)).isoformat()},
                       headers=team["compliance"].headers).json()  # fmt: skip
    client.post(f"{API}/agents/{near['id']}/status", json={"status": "suspended", "reason": "Under investigation now"},
                headers=team["compliance"].headers)  # fmt: skip
    assert far["id"] and agent_review_reminder(session) == 0
