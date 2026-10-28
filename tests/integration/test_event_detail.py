"""One reportable event, and the evidence that it was reported (BRD FR-76; prototype screen 14 "View").

FR-76 asks that evidence of every report is kept as part of the permanent record. The report endpoint
used to accept an evidence document id that no screen ever sent — and it accepted any id at all, so a
file from elsewhere could have been cited as proof. These tests cover both.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.fixtures import (
    API,
    EICAR,
    PDF_BYTES,
    PNG_BYTES,
    assign_cas,
    enrol,
    run_to_precas,
    status_change,
    student_for,
)


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def event(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    student = student_for(client, team["compliance"], draft["id"])
    enrol(client, team["compliance"], student["id"])
    assert status_change(client, team["compliance"], student["id"]).status_code == 200
    rows = client.get(f"{API}/events?student_id={student['id']}", headers=team["compliance"].headers).json()
    return rows[0]


def attach(client, actor, event_id, content=PDF_BYTES, name="sms-confirmation.pdf"):
    return client.post(f"{API}/events/{event_id}/evidence", files={"file": (name, content, "application/pdf")},
                       headers=actor.headers)  # fmt: skip


def test_event_detail_shows_the_event_and_its_history(client, event, team):
    res = client.get(f"{API}/events/{event['id']}", headers=team["admissions"].headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["event"]["event_type"] == "deferral" and body["evidence"] is None
    assert [h["action"] for h in body["history"]] == ["event.created_manual"]


def test_evidence_is_attached_downloaded_and_cited_in_the_report(client, event, team, owner_engine):
    res = attach(client, team["compliance"], event["id"])
    assert res.status_code == 201, res.text
    ev = res.json()["evidence"]
    assert ev["filename"] == "sms-confirmation.pdf"
    got = client.get(f"{API}/documents/{ev['id']}/download", headers=team["compliance"].headers)
    assert got.status_code == 200 and got.content == PDF_BYTES
    rep = client.post(f"{API}/events/{event['id']}/report", json={"sms_reference": "SMS-12345",
                      "reported_date": event["event_date"], "evidence_document_id": ev["id"]},
                      headers=team["compliance"].headers)  # fmt: skip
    assert rep.status_code == 200, rep.text
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT evidence_document_id FROM reportable_events WHERE id = :e"),
                            {"e": event["id"]}).scalar() == ev["id"]  # fmt: skip


def test_evidence_can_be_added_after_the_report(client, event, team):
    client.post(f"{API}/events/{event['id']}/report", json={"sms_reference": "SMS-1", "reported_date": event["event_date"]},
                headers=team["compliance"].headers)  # fmt: skip
    assert attach(client, team["compliance"], event["id"]).status_code == 201


def test_a_replacement_supersedes_the_first_file(client, event, team, owner_engine):
    first = attach(client, team["compliance"], event["id"]).json()["evidence"]["id"]
    second = attach(client, team["compliance"], event["id"], PNG_BYTES, "screenshot.png").json()["evidence"]["id"]
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT superseded_by FROM documents WHERE id = :d"), {"d": first}).scalar() == second


def test_attaching_and_replacing_evidence_is_on_the_events_own_history(client, event, team):
    attach(client, team["compliance"], event["id"], name="first.pdf")
    body = attach(client, team["compliance"], event["id"], PNG_BYTES, "second.png").json()
    assert [h["action"] for h in body["history"]] == ["event.created_manual", "event.evidence", "event.evidence"]
    assert "replacing first.pdf" in body["history"][-1]["detail"]


def test_infected_evidence_is_quarantined(client, event, team):
    res = attach(client, team["compliance"], event["id"], b"%PDF-1.4\n" + EICAR)
    assert res.status_code == 422 and res.json()["error"]["code"] == "ERR_SCAN_FAILED"
    assert client.get(f"{API}/events/{event['id']}", headers=team["compliance"].headers).json()["evidence"] is None


@pytest.mark.parametrize("role", ["admissions", "finance"])
def test_only_the_reporting_roles_attach_evidence(client, event, team, role):
    assert attach(client, team[role], event["id"]).status_code == 403


def test_a_document_from_elsewhere_cannot_be_cited_as_evidence(client, event, team):
    """A real document on the same student's file, but not reporting evidence — and one that does not exist."""
    passport = client.post(f"{API}/students/{event['student_id']}/documents", data={"category": "passport"},
                           files={"file": ("p.pdf", PDF_BYTES, "application/pdf")}, headers=team["compliance"].headers)  # fmt: skip
    assert passport.status_code == 201, passport.text
    for bad in (passport.json()["id"], 999999):
        rep = client.post(f"{API}/events/{event['id']}/report", json={"sms_reference": "SMS-9",
                          "reported_date": event["event_date"], "evidence_document_id": bad},
                          headers=team["compliance"].headers)  # fmt: skip
        assert rep.status_code == 422, rep.text
        assert rep.json()["error"]["details"][0]["field"] == "evidence_document_id"


def test_superseded_evidence_cannot_be_cited(client, event, team):
    first = attach(client, team["compliance"], event["id"]).json()["evidence"]["id"]
    attach(client, team["compliance"], event["id"], PNG_BYTES, "newer.png")
    rep = client.post(f"{API}/events/{event['id']}/report", json={"sms_reference": "SMS-2",
                      "reported_date": event["event_date"], "evidence_document_id": first},
                      headers=team["compliance"].headers)  # fmt: skip
    assert rep.status_code == 422


def test_no_evidence_for_an_event_closed_as_not_reportable(client, event, team):
    client.post(f"{API}/events/{event['id']}/not-required", json={"reason": "Recorded in error, not a deferral."},
                headers=team["senior"].headers)  # fmt: skip
    assert attach(client, team["compliance"], event["id"]).status_code == 409


def test_the_applicant_never_sees_staff_evidence(client, event, team, applicant):
    doc_id = attach(client, team["compliance"], event["id"]).json()["evidence"]["id"]
    record = client.get(f"{API}/applications/{event['application_id']}", headers=applicant.headers).json()
    assert doc_id not in [d["id"] for d in record["documents"]]
    assert client.get(f"{API}/documents/{doc_id}/download", headers=applicant.headers).status_code == 404
