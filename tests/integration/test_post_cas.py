"""The post-CAS world: visa outcomes, enrolment, attendance, status changes, reporting and
metrics (FR-62, FR-71 to FR-100; LLD 7.4).

The properties under test are the ones a compliance visit would probe: that a status change and
its reportable event cannot come apart, that a refused or withdrawn record stays in the CAS
population rather than disappearing from the metrics, and that reporting needs an SMS reference.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from tests.fixtures import (
    API,
    assign_cas,
    attend,
    audit_actions,
    enrol,
    record_visa,
    run_to_precas,
    status_change,
    student_for,
)


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def student(client, applicant, draft, reference, team):
    """An application taken all the way to an assigned CAS, with its student record."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    record = student_for(client, team["compliance"], draft["id"])
    record["application_id"] = draft["id"]
    return record


# --- The record exists from CAS assignment (FR-71) ---------------------------------------------
def test_student_record_starts_awaiting_arrival(client, student, team):
    assert student["enrolment_status"] == "awaiting_arrival"
    assert student["cas_number"]
    assert student["attendance"]["scheduled"] == 0
    assert student["events"] == []


def test_finance_cannot_see_a_student_record(client, student, team):
    r = client.get(f"{API}/students/{student['id']}", headers=team["finance"].headers)
    assert r.status_code == 403


# --- Visa outcomes (FR-85, FR-86, LLD 7.4) ------------------------------------------------------
def test_visa_granted_leaves_the_record_open(client, student, team, owner_engine):
    r = record_visa(client, team["compliance"], student["application_id"], outcome="granted")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolment_status"] == "awaiting_arrival"
    assert body["visa_outcome"]["outcome"] == "granted"
    assert "visa.outcome_recorded" in audit_actions(owner_engine, student["application_id"])


def test_visa_refusal_needs_a_category_and_closes_the_record(client, student, team):
    blocked = record_visa(client, team["compliance"], student["application_id"], outcome="refused")
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "ERR_REASON_REQUIRED"

    ok = record_visa(client, team["compliance"], student["application_id"], outcome="refused",
                     category="credibility")  # fmt: skip
    assert ok.status_code == 200
    body = ok.json()
    assert body["enrolment_status"] == "non_enrolled"
    assert body["enrolment_status_reason"] == "visa_refused"
    # A refusal is not itself reportable — it feeds the refusal metric (LLD 7.4).
    assert body["events"] == []


def test_a_visa_outcome_is_recorded_once(client, student, team):
    assert record_visa(client, team["compliance"], student["application_id"]).status_code == 200
    again = record_visa(client, team["compliance"], student["application_id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "ERR_INVALID_STATE"


def test_only_compliance_or_senior_may_record_a_visa_outcome(client, student, team):
    r = record_visa(client, team["admissions"], student["application_id"])
    assert r.status_code == 403


# --- T-21: CAS withdrawn before a decision -------------------------------------------------------
def test_t21_cas_withdrawal_closes_the_record_and_is_reportable(client, student, team, owner_engine):
    r = client.post(
        f"{API}/applications/{student['application_id']}/cas/withdraw",
        headers=team["senior"].headers,
        json={"reason": "Applicant accepted a place elsewhere and asked us to withdraw the CAS."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolment_status"] == "non_enrolled"
    assert body["enrolment_status_reason"] == "cas_withdrawn"
    assert body["cas_status"] == "withdrawn"
    assert [e["event_type"] for e in body["events"]] == ["cas_withdrawal"]
    assert body["events"][0]["status"] == "pending"

    # The record stays in the CAS population for the metrics, exactly as LLD 7.4 requires.
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM student_records")).scalar_one() == 1
    actions = audit_actions(owner_engine, student["application_id"])
    assert "cas.withdraw" in actions and "event.created_manual" in actions


def test_only_a_senior_may_withdraw_a_cas(client, student, team):
    r = client.post(
        f"{API}/applications/{student['application_id']}/cas/withdraw",
        headers=team["compliance"].headers,
        json={"reason": "A reason long enough to pass validation."},
    )
    assert r.status_code == 403


def test_cas_withdrawal_needs_a_reason(client, student, team):
    r = client.post(
        f"{API}/applications/{student['application_id']}/cas/withdraw",
        headers=team["senior"].headers, json={"reason": "short"},
    )  # fmt: skip
    assert r.status_code == 422


# --- Enrolment and attendance (FR-72 to FR-78) ----------------------------------------------------
def test_enrolment_and_attendance_percentage(client, student, team, owner_engine):
    r = enrol(client, team["compliance"], student["id"],
              permission_expiry=(date.today() + timedelta(days=400)).isoformat())  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["enrolment_status"] == "enrolled"

    for i, status in enumerate(["present", "present", "absent", "authorised_absence"]):
        resp = attend(
            client, team["compliance"], student["id"],
            day=date.today() - timedelta(days=i), module=f"BUS10{i}", status=status,
            reason="Medical appointment, evidence on file." if status == "authorised_absence" else None,
        )  # fmt: skip
        assert resp.status_code == 201, resp.text

    summary = resp.json()["attendance"]
    assert summary["scheduled"] == 4
    assert summary["attended"] == 3  # authorised absence counts towards engagement (FR-76)
    assert summary["percentage"] == 75.0
    assert summary["below_threshold"] is True
    assert "student.attendance" in audit_actions(owner_engine, student["application_id"])


def test_attendance_is_unique_per_student_session_and_module(client, student, team):
    enrol(client, team["compliance"], student["id"])
    assert attend(client, team["compliance"], student["id"]).status_code == 201
    again = attend(client, team["compliance"], student["id"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR_ATTENDANCE_DUPLICATE"


def test_authorised_absence_needs_a_reason(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = attend(client, team["compliance"], student["id"], status="authorised_absence")
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_REASON_REQUIRED"


def test_attendance_cannot_be_recorded_against_a_closed_record(client, student, team):
    record_visa(client, team["compliance"], student["application_id"], outcome="refused", category="finance")
    r = attend(client, team["compliance"], student["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"


def test_future_sessions_are_refused(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = attend(client, team["compliance"], student["id"], day=date.today() + timedelta(days=1))
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_DATE_RANGE"


def test_escalation_is_recorded_against_the_student(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = client.post(f"{API}/students/{student['id']}/escalation", headers=team["compliance"].headers,
                    json={"step": "formal_warning", "notes": "Third missed week."})  # fmt: skip
    assert r.status_code == 201
    assert r.json()["escalations"][0]["step"] == "formal_warning"


# --- T-20: a status change and its event are atomic ------------------------------------------------
def test_t20_deferral_creates_its_reportable_event(client, student, team, owner_engine):
    enrol(client, team["compliance"], student["id"])
    r = status_change(client, team["compliance"], student["id"], change_type="deferral")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolment_status"] == "deferred"
    events = [e for e in body["events"] if e["event_type"] == "deferral"]
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "pending"
    assert event["source"] == "manual"
    assert date.fromisoformat(event["deadline_date"]) > date.fromisoformat(event["event_date"])
    assert "student.status_change" in audit_actions(owner_engine, student["application_id"])


def test_status_change_needs_a_reason_and_leaves_nothing_behind(client, student, team, owner_engine):
    enrol(client, team["compliance"], student["id"])
    r = client.post(f"{API}/students/{student['id']}/status-change", headers=team["compliance"].headers,
                    json={"type": "withdrawal", "effective_date": date.today().isoformat(), "reason": "no"})  # fmt: skip
    assert r.status_code == 422
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM reportable_events")).scalar_one() == 0
        status = conn.execute(text("SELECT enrolment_status FROM student_records")).scalar_one()
    assert status == "enrolled"


def test_withdrawal_closes_the_record_for_completion(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = status_change(client, team["compliance"], student["id"], change_type="withdrawal",
                      reason="Student left the programme and returned home.")  # fmt: skip
    assert r.json()["enrolment_status"] == "withdrawn"
    assert r.json()["completion_outcome"] == "not_completed"
    again = status_change(client, team["compliance"], student["id"], change_type="deferral")
    assert again.status_code == 409


def test_completion_is_recorded(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = client.post(f"{API}/students/{student['id']}/completion", headers=team["compliance"].headers,
                    json={"outcome": "completed", "completion_date": date.today().isoformat()})  # fmt: skip
    assert r.status_code == 200
    assert r.json()["enrolment_status"] == "completed"


# --- The reporting queue (FR-93 to FR-100) ----------------------------------------------------------
def test_reporting_requires_an_sms_reference(client, student, team):
    enrol(client, team["compliance"], student["id"])
    event = status_change(client, team["compliance"], student["id"]).json()["events"][0]
    r = client.post(f"{API}/events/{event['id']}/report", headers=team["compliance"].headers,
                    json={"sms_reference": "  ", "reported_date": date.today().isoformat()})  # fmt: skip
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"ERR_REFERENCE_REQUIRED", "ERR_VALIDATION"}


def test_reporting_an_event_records_who_when_and_the_reference(client, student, team, owner_engine):
    enrol(client, team["compliance"], student["id"])
    event = status_change(client, team["compliance"], student["id"]).json()["events"][0]
    r = client.post(f"{API}/events/{event['id']}/report", headers=team["compliance"].headers,
                    json={"sms_reference": "SMS-2026-99811", "reported_date": date.today().isoformat()})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reported"
    assert body["sms_reference"] == "SMS-2026-99811"
    assert body["reported_by"] == "Staff Compliance"
    assert "event.reported" in audit_actions(owner_engine, student["application_id"])

    again = client.post(f"{API}/events/{event['id']}/report", headers=team["compliance"].headers,
                        json={"sms_reference": "SMS-2", "reported_date": date.today().isoformat()})  # fmt: skip
    assert again.status_code == 409


def test_queue_is_ordered_by_deadline_with_pending_first(client, student, team):
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"], change_type="course_change",
                  effective=date.today() - timedelta(days=5), reason="Moved to the January cohort.")  # fmt: skip
    status_change(client, team["compliance"], student["id"], change_type="change_of_circumstances",
                  reason="Change of UK address notified by the student.")  # fmt: skip
    rows = client.get(f"{API}/events", headers=team["compliance"].headers).json()
    assert [r["status"] for r in rows] == ["pending", "pending"]
    assert rows[0]["deadline_date"] <= rows[1]["deadline_date"]
    assert all(r["application_ref"] for r in rows)


def test_only_a_senior_may_close_an_event_without_reporting(client, student, team):
    enrol(client, team["compliance"], student["id"])
    event = status_change(client, team["compliance"], student["id"]).json()["events"][0]
    refused = client.post(f"{API}/events/{event['id']}/not-required", headers=team["compliance"].headers,
                          json={"reason": "Reviewed and found not reportable under the guidance."})  # fmt: skip
    assert refused.status_code == 403
    ok = client.post(f"{API}/events/{event['id']}/not-required", headers=team["senior"].headers,
                     json={"reason": "Reviewed and found not reportable under the guidance."})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "not_required"


# --- T-14: BCA metrics --------------------------------------------------------------------------------
def test_t14_refusal_rate_reports_counts_when_the_sample_is_small(client, student, team):
    record_visa(client, team["compliance"], student["application_id"], outcome="refused", category="finance")
    body = client.get(f"{API}/metrics/bca", headers=team["senior"].headers).json()
    refusal = next(m for m in body["metrics"] if m["key"] == "refusal")
    assert refusal["numerator"] == 1 and refusal["denominator"] == 1
    assert refusal["percentage"] == 100.0
    assert refusal["rag"] == "red"
    assert refusal["small_sample"] is True
    assert "1 of 1" in refusal["note"]
    assert body["overall_rag"] == "red"  # the worst of the three, never an average


def test_metrics_are_green_when_nothing_has_gone_wrong(client, student, team):
    enrol(client, team["compliance"], student["id"])
    record_visa(client, team["compliance"], student["application_id"], outcome="granted")
    body = client.get(f"{API}/metrics/bca", headers=team["senior"].headers).json()
    assert {m["key"] for m in body["metrics"]} == {"refusal", "enrolment", "completion"}
    assert body["overall_rag"] == "green"


# --- T-18: the evidence pack ----------------------------------------------------------------------------
def test_t18_evidence_pack_contains_the_whole_file(client, student, team, owner_engine):
    import io
    import zipfile

    from tests.fixtures import pdf_text

    enrol(client, team["compliance"], student["id"])
    attend(client, team["compliance"], student["id"], module="BUS701")
    status_change(client, team["compliance"], student["id"], change_type="deferral")
    r = client.post(f"{API}/applications/{student['application_id']}/evidence-pack",
                    headers=team["senior"].headers)  # fmt: skip
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "ready", job
    assert job["expires_at"]

    download = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = archive.namelist()
    assert any(n.endswith("00-evidence-pack-index.pdf") for n in names)
    manifest = archive.read(next(n for n in names if n.endswith("manifest.txt"))).decode()
    assert "Audit entries:" in manifest
    assert "correspondence log" in manifest  # the pack says what it does not contain

    index = pdf_text(archive.read(next(n for n in names if n.endswith(".pdf"))))
    # T-18: all documents, both sign-offs with names, events and the full audit trail.
    for expected in ["Evidence pack", "Academic assessment", "Compliance review", "Staff Admissions",
                     "Staff Compliance", "CAS and visa outcome", "Reportable events", "Deferral",
                     "Audit trail", "cas.assign", "student.attendance"]:  # fmt: skip
        assert expected in index, f"{expected} missing from the evidence pack index"
    assert "data.evidence_pack" in audit_actions(owner_engine, student["application_id"])

    again = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    assert again.status_code == 200
    downloads = [a for a in audit_actions(owner_engine, student["application_id"]) if a == "data.evidence_pack"]
    assert len(downloads) >= 3  # built once, downloaded twice


def test_an_evidence_pack_is_a_senior_action(client, student, team):
    """LLD 6: the pack carries the payment history, which compliance and admissions are not shown."""
    for actor in (team["compliance"], team["admissions"], team["finance"]):
        r = client.post(f"{API}/applications/{student['application_id']}/evidence-pack", headers=actor.headers)
        assert r.status_code == 403, actor.email


def test_an_evidence_pack_download_belongs_to_the_person_who_asked_for_it(client, student, team, staff):
    job = client.post(f"{API}/applications/{student['application_id']}/evidence-pack",
                      headers=team["senior"].headers).json()  # fmt: skip
    other_senior = staff("senior", "second.senior@staff.test")
    assert client.get(f"{API}/jobs/{job['id']}/download", headers=other_senior.headers).status_code == 404


# --- Authorisation on the post-CAS endpoints (LLD 6) --------------------------------------------------
def test_admissions_cannot_raise_a_reportable_event_or_close_a_course(client, student, team):
    enrol(client, team["compliance"], student["id"])
    change = client.post(f"{API}/students/{student['id']}/status-change", headers=team["admissions"].headers,
                         json={"type": "deferral", "effective_date": date.today().isoformat(),
                               "reason": "Deferring to the next intake on request."})  # fmt: skip
    assert change.status_code == 403
    completion = client.post(f"{API}/students/{student['id']}/completion", headers=team["admissions"].headers,
                             json={"outcome": "completed", "completion_date": date.today().isoformat()})  # fmt: skip
    assert completion.status_code == 403


def test_metrics_are_not_open_to_finance_or_admissions(client, student, team):
    for actor in (team["finance"], team["admissions"]):
        assert client.get(f"{API}/metrics/bca", headers=actor.headers).status_code == 403
    assert client.get(f"{API}/metrics/bca", headers=team["compliance"].headers).status_code == 200


# --- A closed record stays closed (LLD 7.4) -----------------------------------------------------------
def test_a_change_of_circumstances_cannot_resurrect_a_closed_record(client, student, team):
    """Regression: course_change and change_of_circumstances used to set the record to enrolled,
    which would have put a refused applicant back into the enrolment rate."""
    record_visa(client, team["compliance"], student["application_id"], outcome="refused", category="finance")
    r = client.post(f"{API}/students/{student['id']}/status-change", headers=team["compliance"].headers,
                    json={"type": "change_of_circumstances", "effective_date": date.today().isoformat(),
                          "reason": "New correspondence address supplied by the applicant."})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"

    metrics = client.get(f"{API}/metrics/bca", headers=team["compliance"].headers).json()
    enrolment = next(m for m in metrics["metrics"] if m["key"] == "enrolment")
    assert enrolment["numerator"] == 0


def test_a_course_change_leaves_the_enrolment_status_alone(client, student, team):
    enrol(client, team["compliance"], student["id"])
    r = status_change(client, team["compliance"], student["id"], change_type="course_change",
                      reason="Moved to the evening delivery of the same programme.")  # fmt: skip
    assert r.status_code == 200
    assert r.json()["enrolment_status"] == "enrolled"
    assert [e["event_type"] for e in r.json()["events"]] == ["course_change"]


# --- The student list -----------------------------------------------------------------------------
def test_student_list_filters_and_counts_open_events(client, student, team, owner_engine):
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"], change_type="interruption",
                  reason="Interrupted for one term on medical advice.")  # fmt: skip

    everything = client.get(f"{API}/students", headers=team["compliance"].headers).json()
    assert everything["total"] == 1
    row = everything["items"][0]
    assert row["student_name"] == "Priya Raman"
    assert row["enrolment_status"] == "interrupted"
    assert row["open_events"] == 1 and row["overdue_events"] == 0

    by_status = client.get(f"{API}/students?status=enrolled", headers=team["compliance"].headers).json()
    assert by_status["total"] == 0
    by_name = client.get(f"{API}/students?q=priya", headers=team["compliance"].headers).json()
    assert by_name["total"] == 1
    by_ref = client.get(f"{API}/students?q={student['application_ref'][-6:]}",
                        headers=team["compliance"].headers).json()  # fmt: skip
    assert by_ref["total"] == 1

    with owner_engine.connect() as conn:
        conn.execute(text(
            "UPDATE reportable_events SET event_date = current_date - 20, deadline_date = current_date - 2"))
        conn.commit()
    overdue = client.get(f"{API}/students", headers=team["compliance"].headers).json()["items"][0]
    assert overdue["overdue_events"] == 1


def test_an_application_without_a_cas_has_no_student_record(client, applicant, draft, reference, team):
    from tests.fixtures import submit_application

    submit_application(client, applicant, draft["id"], reference)
    r = client.get(f"{API}/applications/{draft['id']}/student", headers=team["compliance"].headers)
    assert r.status_code == 404


def test_an_unknown_student_is_not_found(client, team):
    assert client.get(f"{API}/students/99999", headers=team["compliance"].headers).status_code == 404


def test_the_queue_can_be_filtered_to_one_student(client, student, team):
    enrol(client, team["compliance"], student["id"])
    status_change(client, team["compliance"], student["id"])
    mine = client.get(f"{API}/events?student_id={student['id']}", headers=team["compliance"].headers).json()
    assert len(mine) == 1
    other = client.get(f"{API}/events?student_id=99999", headers=team["compliance"].headers).json()
    assert other == []
