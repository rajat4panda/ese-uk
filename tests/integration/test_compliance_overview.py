"""Compliance classification on the server (BRD FR-70, FR-71; prototype screens 13 and 15).

Before this, a student with poor attendance or a visa about to lapse counted as "compliant" until an
event was raised by hand. These tests pin each rule to its reason, and check the worst issue wins.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from tests.fixtures import API, assign_cas, attend, enrol, run_to_precas, status_change, student_for


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def student(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    return student_for(client, team["compliance"], draft["id"])


def overview(client, actor) -> dict:
    res = client.get(f"{API}/compliance/overview", headers=actor.headers)
    assert res.status_code == 200, res.text
    return res.json()


RECORD_KEEPING = {"status_check_missing", "address_update", "enrolment_evidence_missing"}


def kinds(data: dict) -> list[str]:
    """The issues these tests are about. The record-keeping flags (no right-to-study check, no UK
    address, no enrolment evidence) have their own tests in test_student_record."""
    return [i["kind"] for s in data["students"] for i in s["issues"] if i["kind"] not in RECORD_KEEPING]


def fully_recorded(client, team, student_id: int, expiry: date) -> None:
    """Everything a sponsor should hold for an enrolled student, so only the rule under test can flag."""
    from tests.fixtures import PDF_BYTES

    doc = client.post(f"{API}/students/{student_id}/documents", data={"category": "enrolment_evidence"},
                      files={"file": ("e.pdf", PDF_BYTES, "application/pdf")}, headers=team["compliance"].headers)  # fmt: skip
    enrol(client, team["compliance"], student_id, evidence_document_id=doc.json()["id"])
    assert client.post(f"{API}/students/{student_id}/status-checks", headers=team["compliance"].headers, json={
        "check_date": date.today().isoformat(), "method": "brp", "reference": "ZR1234567",
        "permission_expiry": expiry.isoformat(), "outcome": "confirmed"}).status_code == 201  # fmt: skip
    assert client.post(f"{API}/students/{student_id}/contact", headers=team["compliance"].headers, json={
        "effective_from": date.today().isoformat(),
        "address": {"line1": "20 Bishopsgate", "city": "London", "postcode": "EC2N 4AJ", "country": "GB"},
    }).status_code == 201  # fmt: skip


def backdate_events(owner_engine) -> None:
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE reportable_events SET event_date = current_date - 30, deadline_date = current_date - 1"))
        conn.commit()


def test_a_student_with_nothing_wrong_is_compliant(client, student, team):
    fully_recorded(client, team, student["id"], date.today() + timedelta(days=400))
    for _ in range(5):
        attend(client, team["compliance"], student["id"])
    data = overview(client, team["compliance"])
    assert (data["total"], data["compliant"], data["attention"], data["critical"]) == (1, 1, 0, 0)


def test_low_attendance_needs_attention_with_the_reason(client, student, team):
    enrol(client, team["compliance"], student["id"])
    for n in range(5):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=n),
               module=f"M{n}", status="present" if n < 2 else "absent")  # fmt: skip
    data = overview(client, team["compliance"])
    assert data["attention"] == 1
    issue = data["students"][0]["issues"][0]
    assert issue["kind"] == "attendance" and issue["label"] == "Engagement concern"
    assert "40.0%" in issue["detail"] and "below 80%" in issue["detail"]


def test_the_threshold_setting_moves_the_line(client, student, team):
    enrol(client, team["compliance"], student["id"])
    for n in range(4):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=n),
               module=f"M{n}", status="present" if n < 3 else "absent")  # fmt: skip
    assert "attendance" in kinds(overview(client, team["compliance"]))  # 75% < 80%
    client.put(f"{API}/admin/settings/ATTENDANCE_THRESHOLD_PCT", json={"value": "70"}, headers=team["senior"].headers)
    assert "attendance" not in kinds(overview(client, team["compliance"]))  # 75% >= 70%


def test_visa_expiring_soon_needs_attention_and_expired_is_critical(client, student, team, owner_engine):
    fully_recorded(client, team, student["id"], date.today() + timedelta(days=20))
    data = overview(client, team["compliance"])
    assert kinds(data) == ["visa_expiring"] and data["attention"] == 1
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE student_records SET permission_expiry = current_date - 1"))
        conn.commit()
    data = overview(client, team["compliance"])
    assert kinds(data) == ["visa_expired"] and data["critical"] == 1


def test_an_open_event_needs_attention_and_an_overdue_one_is_critical(client, student, team, owner_engine):
    enrol(client, team["compliance"], student["id"])
    assert status_change(client, team["compliance"], student["id"], change_type="course_change").status_code == 200
    data = overview(client, team["compliance"])
    assert "event_open" in kinds(data) and data["attention"] == 1
    backdate_events(owner_engine)
    data = overview(client, team["compliance"])
    assert data["critical"] == 1 and data["students"][0]["severity"] == "critical"
    assert data["students"][0]["issues"][0]["kind"] == "event_overdue"  # worst first


def test_a_withdrawal_stays_on_the_list_until_it_is_reported(client, student, team, owner_engine):
    """The withdrawal closes the record at the moment it raises the event that must be reported.
    Leaving closed records out would hide exactly that report."""
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"], change_type="withdrawal",
                  reason="Student withdrew from the course by email.")  # fmt: skip
    data = overview(client, team["compliance"])
    assert data["total"] == 1 and kinds(data) == ["event_open"]
    backdate_events(owner_engine)
    assert overview(client, team["compliance"])["critical"] == 1
    event_id = client.get(f"{API}/events?student_id={student['id']}", headers=team["compliance"].headers).json()[0]["id"]
    client.post(f"{API}/events/{event_id}/report", json={"sms_reference": "SMS-W1",
                "reported_date": (date.today() - timedelta(days=1)).isoformat()}, headers=team["compliance"].headers)  # fmt: skip
    assert overview(client, team["compliance"])["total"] == 0  # reported, and the record is closed


def test_issue_counts_feed_the_dashboard_alerts(client, student, team):
    fully_recorded(client, team, student["id"], date.today() + timedelta(days=10))
    assert overview(client, team["admissions"])["issue_counts"] == {"visa_expiring": 1}


def test_finance_and_applicants_cannot_read_it(client, reference, team, applicant):
    assert client.get(f"{API}/compliance/overview", headers=team["finance"].headers).status_code == 403
    assert client.get(f"{API}/compliance/overview", headers=applicant.headers).status_code == 403
