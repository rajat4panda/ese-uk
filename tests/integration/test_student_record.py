"""The student record after CAS: right-to-study checks (BRD FR-68), evidence of enrolment (FR-61),
address and contact history (FR-67) and the CAS withdrawal screen (FR-58).

Before this sprint "last status check" was stamped with today's date whenever any status changed, so
the record claimed checks nobody had made; enrolment evidence could be cited but never seen; and the
address was the one copied from the application at CAS, with no way to change it or see what it was.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from tests.fixtures import API, PDF_BYTES, assign_cas, enrol, pdf_text, run_to_precas, status_change, student_for

TODAY = date.today()
UK_ADDRESS = {"line1": "Flat 3, 20 Bishopsgate", "city": "London", "postcode": "EC2N 4AJ", "country": "GB"}


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def ctx(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    return {"app_id": draft["id"], "student": student_for(client, team["compliance"], draft["id"])}


def upload(client, actor, student_id, category="enrolment_evidence", name="enrolment-confirmation.pdf"):
    res = client.post(f"{API}/students/{student_id}/documents", data={"category": category},
                      files={"file": (name, PDF_BYTES, "application/pdf")}, headers=actor.headers)  # fmt: skip
    assert res.status_code == 201, res.text
    return res.json()["id"]


def check(client, actor, student_id, **body):
    payload = {"check_date": TODAY.isoformat(), "method": "evisa_share_code", "reference": "w4k 9p2 7xa",
               "permission_type": "Student", "permission_expiry": (TODAY + timedelta(days=400)).isoformat(),
               "outcome": "confirmed"}  # fmt: skip
    payload.update(body)
    return client.post(f"{API}/students/{student_id}/status-checks", json=payload, headers=actor.headers)


def contact(client, actor, student_id, **body):
    payload = {"effective_from": TODAY.isoformat(), "address": UK_ADDRESS, "phone": "+44 7700 900123",
               "email": "Student@Example.com", "reason": "Moved into university halls."}  # fmt: skip
    payload.update(body)
    return client.post(f"{API}/students/{student_id}/contact", json=payload, headers=actor.headers)


def overview_kinds(client, actor) -> list[str]:
    data = client.get(f"{API}/compliance/overview", headers=actor.headers).json()
    return [i["kind"] for s in data["students"] for i in s["issues"]]


# --- Right-to-study checks (FR-68) ---------------------------------------------------------------------
def test_a_confirmed_check_is_recorded_and_sets_the_permission_expiry(client, ctx, team, owner_engine):
    sid = ctx["student"]["id"]
    res = check(client, team["compliance"], sid)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["reference"] == "W4K9P27XA" and body["outcome_label"] == "Right to study confirmed"
    student = client.get(f"{API}/students/{sid}", headers=team["compliance"].headers).json()
    assert student["last_status_check"] == TODAY.isoformat()
    assert student["permission_expiry"] == (TODAY + timedelta(days=400)).isoformat()
    assert student["latest_status_check_outcome"] == "confirmed"
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action = 'student.status_check'")).scalar()
    assert "W4K9P27XA" in detail and "right to study confirmed" in detail


def test_no_check_is_claimed_until_one_is_made(client, ctx, team):
    """Status changes used to stamp today's date as the last check."""
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid)
    status_change(client, team["compliance"], sid, change_type="course_change")
    assert client.get(f"{API}/students/{sid}", headers=team["compliance"].headers).json()["last_status_check"] is None


@pytest.mark.parametrize(("body", "field"), [
    ({"reference": "ABC"}, "reference"),
    ({"permission_expiry": None}, "permission_expiry"),
    ({"permission_expiry": (TODAY - timedelta(days=1)).isoformat()}, "permission_expiry"),
    ({"check_date": (TODAY + timedelta(days=1)).isoformat()}, "check_date"),
    ({"outcome": "discrepancy"}, "notes"),
    ({"evidence_document_id": 999999}, "evidence_document_id"),
])  # fmt: skip
def test_a_check_must_be_complete_and_consistent(client, ctx, team, body, field):
    res = check(client, team["compliance"], ctx["student"]["id"], **body)
    assert res.status_code == 422, res.text
    assert res.json()["error"]["details"][0]["field"] == field


def test_an_older_check_recorded_late_does_not_roll_the_record_back(client, ctx, team):
    sid = ctx["student"]["id"]
    check(client, team["compliance"], sid)
    late = check(client, team["compliance"], sid, check_date=(TODAY - timedelta(days=30)).isoformat(),
                 method="brp", reference="ZR1234567", permission_expiry=(TODAY + timedelta(days=100)).isoformat())  # fmt: skip
    assert late.status_code == 201, late.text
    student = client.get(f"{API}/students/{sid}", headers=team["compliance"].headers).json()
    assert student["permission_expiry"] == (TODAY + timedelta(days=400)).isoformat()
    rows = client.get(f"{API}/students/{sid}/status-checks", headers=team["admissions"].headers).json()
    assert [r["method"] for r in rows] == ["evisa_share_code", "brp"]  # newest first


def test_a_check_with_evidence_on_file(client, ctx, team):
    sid = ctx["student"]["id"]
    doc = upload(client, team["compliance"], sid, category="other", name="share-code-result.pdf")
    res = check(client, team["compliance"], sid, evidence_document_id=doc)
    assert res.status_code == 201 and res.json()["evidence"]["filename"] == "share-code-result.pdf"


def test_checks_are_append_only_in_the_database(client, ctx, team):
    check(client, team["compliance"], ctx["student"]["id"])
    engine = create_engine(get_settings().DATABASE_URL)
    with engine.connect() as conn:
        for sql in ("UPDATE status_checks SET outcome = 'no_permission'", "DELETE FROM status_checks",
                    "UPDATE student_contact_changes SET phone = NULL"):  # fmt: skip
            with pytest.raises(DBAPIError):
                conn.execute(text(sql))
            conn.rollback()
    engine.dispose()


def test_finance_cannot_see_or_record_checks(client, ctx, team):
    sid = ctx["student"]["id"]
    assert check(client, team["finance"], sid).status_code == 403
    assert client.get(f"{API}/students/{sid}/status-checks", headers=team["finance"].headers).status_code == 403


# --- Enrolment evidence (FR-61) ------------------------------------------------------------------------
def test_enrolment_with_evidence_shows_the_file(client, ctx, team):
    sid = ctx["student"]["id"]
    doc = upload(client, team["compliance"], sid)
    res = enrol(client, team["compliance"], sid, evidence_document_id=doc)
    assert res.status_code == 200, res.text
    assert res.json()["enrolment_evidence"] == {"id": doc, "filename": "enrolment-confirmation.pdf"}


def test_evidence_must_be_enrolment_evidence(client, ctx, team):
    sid = ctx["student"]["id"]
    passport = upload(client, team["compliance"], sid, category="passport", name="passport.pdf")
    res = enrol(client, team["compliance"], sid, evidence_document_id=passport)
    assert res.status_code == 422 and res.json()["error"]["details"][0]["field"] == "evidence_document_id"


def test_evidence_can_be_attached_after_enrolment_and_is_audited(client, ctx, team, owner_engine):
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid)
    assert "enrolment_evidence_missing" in overview_kinds(client, team["compliance"])
    doc = upload(client, team["compliance"], sid)
    res = client.post(f"{API}/students/{sid}/enrolment-evidence", json={"document_id": doc},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["enrolment_evidence"]["id"] == doc
    assert "enrolment_evidence_missing" not in overview_kinds(client, team["compliance"])
    again = client.post(f"{API}/students/{sid}/enrolment-evidence", json={"document_id": doc},
                        headers=team["compliance"].headers)  # fmt: skip
    assert again.status_code == 409
    with owner_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM audit_entries WHERE action = 'student.enrolment_evidence'")).scalar()
    assert n == 1


def test_evidence_before_enrolment_is_refused(client, ctx, team):
    sid = ctx["student"]["id"]
    doc = upload(client, team["compliance"], sid)
    res = client.post(f"{API}/students/{sid}/enrolment-evidence", json={"document_id": doc},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 409


# --- Address and contact history (FR-67) ---------------------------------------------------------------
def test_contact_details_start_from_the_application(client, ctx, team):
    student = ctx["student"]
    assert student["current_address"]["city"] == "Kochi"
    assert student["contact_email"]


def test_a_change_updates_the_record_and_keeps_the_history(client, ctx, team, owner_engine):
    sid = ctx["student"]["id"]
    res = contact(client, team["compliance"], sid)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["current_address"]["postcode"] == "EC2N 4AJ"
    assert (body["contact_phone"], body["contact_email"]) == ("+447700900123", "student@example.com")
    history = client.get(f"{API}/students/{sid}/contact", headers=team["admissions"].headers).json()
    assert len(history) == 1 and history[0]["reason"] == "Moved into university halls."
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action = 'student.contact_change'")).scalar()
    assert "Kochi" in detail and "EC2N 4AJ" in detail


def test_an_older_change_recorded_late_joins_the_history_only(client, ctx, team):
    sid = ctx["student"]["id"]
    contact(client, team["compliance"], sid)
    old = {**UK_ADDRESS, "line1": "1 Temporary Lodgings", "postcode": "E1 6AN"}
    contact(client, team["compliance"], sid, effective_from=(TODAY - timedelta(days=20)).isoformat(), address=old)
    student = client.get(f"{API}/students/{sid}", headers=team["compliance"].headers).json()
    assert student["current_address"]["postcode"] == "EC2N 4AJ"
    history = client.get(f"{API}/students/{sid}/contact", headers=team["compliance"].headers).json()
    assert [h["address"]["postcode"] for h in history] == ["EC2N 4AJ", "E1 6AN"]


@pytest.mark.parametrize("body", [
    {"address": {"line1": "No city", "postcode": "E1 6AN", "country": "GB"}},
    {"phone": "07700 900123"},
    {"email": "not-an-email"},
    {"effective_from": (TODAY + timedelta(days=2)).isoformat()},
])  # fmt: skip
def test_a_change_must_be_complete_and_well_formed(client, ctx, team, body):
    assert contact(client, team["compliance"], ctx["student"]["id"], **body).status_code == 422


def test_a_uk_address_clears_the_address_flag(client, ctx, team):
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid)
    assert "address_update" in overview_kinds(client, team["compliance"])
    contact(client, team["compliance"], sid)
    assert "address_update" not in overview_kinds(client, team["compliance"])


# --- Monitoring: the right-to-study flags --------------------------------------------------------------
def test_an_enrolled_student_with_no_check_needs_attention(client, ctx, team):
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid)
    assert "status_check_missing" in overview_kinds(client, team["compliance"])
    check(client, team["compliance"], sid)
    assert "status_check_missing" not in overview_kinds(client, team["compliance"])


def test_a_failed_check_is_critical(client, ctx, team):
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid)
    check(client, team["compliance"], sid, outcome="no_permission", permission_expiry=None,
          notes="Share code shows the permission was curtailed.")  # fmt: skip
    data = client.get(f"{API}/compliance/overview", headers=team["compliance"].headers).json()
    assert data["critical"] == 1
    assert data["students"][0]["issues"][0]["kind"] == "status_check_failed"


def test_a_fully_recorded_student_is_compliant(client, ctx, team):
    sid = ctx["student"]["id"]
    enrol(client, team["compliance"], sid, evidence_document_id=upload(client, team["compliance"], sid))
    check(client, team["compliance"], sid)
    contact(client, team["compliance"], sid)
    data = client.get(f"{API}/compliance/overview", headers=team["compliance"].headers).json()
    assert (data["total"], data["compliant"]) == (1, 1), data["students"]


# --- CAS withdrawal (FR-58) ----------------------------------------------------------------------------
def withdraw(client, actor, app_id, **body):
    payload = {"reason": "Student has decided not to come to the UK."}
    payload.update(body)
    return client.post(f"{API}/applications/{app_id}/cas/withdraw", json=payload, headers=actor.headers)


def test_cas_cancelled_with_an_earlier_effective_date(client, ctx, team, owner_engine):
    res = withdraw(client, team["senior"], ctx["app_id"], withdrawal_type="cancelled",
                   withdrawal_date=TODAY.isoformat(), reason="Issued with the wrong course dates; to be reissued.")  # fmt: skip
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["cas_withdrawal_type"], body["cas_withdrawal_date"]) == ("cancelled", TODAY.isoformat())
    assert body["cas_withdrawal_reason"].startswith("Issued with the wrong")
    assert body["enrolment_status_reason"] == "cas_cancelled"
    event = body["events"][0]
    assert event["event_type"] == "cas_withdrawal" and event["event_date"] == TODAY.isoformat()
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action = 'cas.withdraw'")).scalar()
    assert "cancelled with effect from" in detail


@pytest.mark.parametrize("when", [TODAY + timedelta(days=1), TODAY - timedelta(days=30)])
def test_the_withdrawal_date_must_lie_between_assignment_and_today(client, ctx, team, when):
    res = withdraw(client, team["senior"], ctx["app_id"], withdrawal_date=when.isoformat())
    assert res.status_code == 422 and res.json()["error"]["details"][0]["field"] == "withdrawal_date"


def test_only_the_authorising_officer_withdraws(client, ctx, team):
    assert withdraw(client, team["compliance"], ctx["app_id"]).status_code == 403


def test_the_evidence_pack_shows_checks_contacts_and_withdrawal(client, ctx, team):
    sid = ctx["student"]["id"]
    check(client, team["compliance"], sid)
    contact(client, team["compliance"], sid)
    withdraw(client, team["senior"], ctx["app_id"], withdrawal_type="cancelled")
    import io
    import zipfile

    job = client.post(f"{API}/applications/{ctx['app_id']}/evidence-pack", headers=team["senior"].headers).json()
    download = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    content = pdf_text(archive.read(next(n for n in archive.namelist() if n.endswith("index.pdf"))))
    assert "Right-to-study checks" in content and "W4K9P27XA" in content
    assert "Address and contact history" in content and "EC2N 4AJ" in content
    assert "cancelled with effect from" in content


# --- Found by the independent review --------------------------------------------------------------------
def test_the_applicants_own_view_never_lists_staff_files(client, ctx, team, applicant):
    """POST /applications returns the open application, which after CAS still holds the staff files."""
    staff_doc = upload(client, team["compliance"], ctx["student"]["id"])
    for res in (client.post(f"{API}/applications", headers=applicant.headers),
                client.get(f"{API}/applications/{ctx['app_id']}", headers=applicant.headers)):  # fmt: skip
        assert res.status_code in (200, 201), res.text
        assert staff_doc not in [d["id"] for d in res.json()["documents"]]


def test_a_backdated_contact_change_does_not_claim_to_change_the_record(client, ctx, team, owner_engine):
    sid = ctx["student"]["id"]
    contact(client, team["compliance"], sid)
    old = {**UK_ADDRESS, "line1": "1 Temporary Lodgings", "postcode": "E1 6AN"}
    contact(client, team["compliance"], sid, effective_from=(TODAY - timedelta(days=20)).isoformat(), address=old)
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action = 'student.contact_change' "
                                   "ORDER BY id DESC LIMIT 1")).scalar()  # fmt: skip
    assert "->" not in detail and "E1 6AN" in detail and "current details unchanged" in detail
