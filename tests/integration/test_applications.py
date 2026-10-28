"""Applicant journey (LLD 5.2, 7, 8.1, 8.8, 9; Appendix A)."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.services import cache, notifications
from app.services.auth import decode_token
from tests.fixtures import API, audit_actions, complete_data, login, register


def put(client, actor, app_id, data, etag=None):
    headers = dict(actor.headers)
    if etag:
        headers["If-Match"] = etag
    return client.put(f"{API}/applications/{app_id}", json={"data": data}, headers=headers)


# --- Creation and reference (FR-03, FR-06) --------------------------------------------------
def test_create_draft_with_reference(client, applicant, owner_engine):
    r = client.post(f"{API}/applications", headers=applicant.headers)
    assert r.status_code == 201
    app = r.json()
    assert re.fullmatch(rf"ESE-{date.today().year}-\d{{6}}", app["ref"])
    assert app["ref"].endswith("000127") or int(app["ref"][-6:]) >= 127  # sequence starts at 127 (LLD 8.1)
    assert app["status"] == "draft" and app["editable"] is True
    assert app["data"] == {"full_name": "Priya Raman", "email": "priya@example.com"}
    assert audit_actions(owner_engine, app["id"]) == ["application.create"]


def test_second_create_returns_existing_open_application(client, applicant, draft):
    r = client.post(f"{API}/applications", headers=applicant.headers)
    assert r.status_code == 200
    assert r.json()["id"] == draft["id"]


def test_references_unique_and_sequential(client):
    refs = []
    for i in range(3):
        a = register(client, f"seq{i}@example.com", verify=False)
        refs.append(client.post(f"{API}/applications", headers=a.headers).json()["ref"])
    nums = [int(r[-6:]) for r in refs]
    assert nums == sorted(nums) and len(set(nums)) == 3


def test_staff_cannot_create_application(client, staff):
    s = staff("admissions")
    assert client.post(f"{API}/applications", headers=s.headers).status_code == 403


# --- Autosave (FR-04) ------------------------------------------------------------------------
def test_autosave_merges_and_normalises(client, applicant, draft, reference, owner_engine):
    r = put(client, applicant, draft["id"], {"passport_number": " ab 12345 ", "nationality": "in",
                                             "mobile": "+44 7700 900123"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["passport_number"] == "AB12345"
    assert d["nationality"] == "IN"
    assert d["mobile"] == "+447700900123"
    assert d["full_name"] == "Priya Raman"  # untouched keys kept
    r2 = put(client, applicant, draft["id"], {"programme_id": reference["programme_id"],
                                              "intake_id": reference["intake_id"]})
    body = r2.json()
    assert body["programme_id"] == reference["programme_id"] and body["intake_id"] == reference["intake_id"]
    assert audit_actions(owner_engine, draft["id"]).count("application.save") == 2


def test_autosave_without_change_writes_nothing(client, applicant, draft, owner_engine):
    put(client, applicant, draft["id"], {"nationality": "IN"})
    put(client, applicant, draft["id"], {"nationality": "IN"})
    assert audit_actions(owner_engine, draft["id"]).count("application.save") == 1


def test_clearing_a_field(client, applicant, draft):
    put(client, applicant, draft["id"], {"other_names": "Priya R"})
    r = put(client, applicant, draft["id"], {"other_names": ""})
    assert "other_names" not in r.json()["data"]


def test_autosave_rejects_malformed_values_with_field_and_step(client, applicant, draft):
    r = put(client, applicant, draft["id"], {"passport_number": "ab-12", "mobile": "0770090",
                                             "nationality": "XX", "home_address": {"country": "ZZ"}})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "ERR_VALIDATION"
    by_field = {d["field"]: d for d in err["details"]}
    assert by_field["passport_number"]["code"] == "ERR_INVALID_FORMAT" and by_field["passport_number"]["step"] == 2
    assert by_field["mobile"]["step"] == 1
    assert "nationality" in by_field
    assert by_field["home_address.country"]["step"] == 3


def test_autosave_rejects_server_and_unknown_keys(client, applicant, draft):
    for key in ("submitted_at", "status", "privacy_notice_version", "role"):
        r = put(client, applicant, draft["id"], {key: "x"})
        assert r.status_code == 422, key
        assert r.json()["error"]["details"][0]["code"] == "ERR_UNKNOWN_FIELD"


def test_autosave_validates_references(client, applicant, draft, reference):
    r = put(client, applicant, draft["id"], {"programme_id": 999999, "intake_id": reference["closed_intake_id"],
                                             "agent_id": reference["suspended_agent_id"]})
    assert r.status_code == 422
    fields = {d["field"]: d["code"] for d in r.json()["error"]["details"]}
    assert fields == {"programme_id": "ERR_INVALID_REFERENCE", "intake_id": "ERR_INVALID_REFERENCE",
                      "agent_id": "ERR_INVALID_REFERENCE"}


def test_email_must_match_account(client, applicant, draft):
    r = put(client, applicant, draft["id"], {"email": "someone.else@example.com"})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "ERR_EMAIL_MISMATCH"


def test_if_match_stale_record(client, applicant, draft):
    first = put(client, applicant, draft["id"], {"nationality": "IN"}, etag=f'"{draft["updated_at"]}"')
    assert first.status_code == 200
    stale = put(client, applicant, draft["id"], {"nationality": "GB"}, etag=f'"{draft["updated_at"]}"')
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ERR_STALE_RECORD"
    fresh = put(client, applicant, draft["id"], {"nationality": "GB"}, etag=first.headers["ETag"])
    assert fresh.status_code == 200


@pytest.mark.priority("T-28")
def test_t28_draft_survives_session_timeout(client, applicant, draft, reference):
    """T-28: draft autosave then session timeout -> entered data present on next sign-in (FR-04)."""
    put(client, applicant, draft["id"], {"passport_number": "N1234567", "study_mode": "full_time"})
    cache.get_redis().delete(f"session:{decode_token(applicant.token).jti}")  # inactivity timeout
    assert client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).status_code == 401
    again = login(client, applicant.email)
    r = client.post(f"{API}/applications", headers=again.headers)  # resume
    assert r.status_code == 200
    assert r.json()["data"]["passport_number"] == "N1234567"
    assert r.json()["data"]["study_mode"] == "full_time"


# --- Submission (FR-05, FR-07, FR-08) -----------------------------------------------------------
@pytest.mark.priority("T-01")
def test_t01_missing_nationality_and_passport(client, applicant, draft, reference, owner_engine):
    """T-01: 422 ERR_MANDATORY_FIELDS listing both fields; status unchanged."""
    data = complete_data(reference)
    del data["nationality"], data["passport_number"]
    assert put(client, applicant, draft["id"], data).status_code == 200
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "ERR_MANDATORY_FIELDS"
    assert {d["field"] for d in err["details"]} == {"nationality", "passport_number"}
    assert all(d["step"] == 2 and d["code"] == "ERR_REQUIRED" for d in err["details"])
    status = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()["status"]
    assert status == "draft"
    assert "application.submit" not in audit_actions(owner_engine, draft["id"])


def test_submit_reports_every_problem_at_once(client, applicant, draft, reference):
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    for f in ("date_of_birth", "mobile", "nationality", "passport_number", "home_address", "programme_id",
              "intake_id", "qualifications", "english_route", "been_to_uk", "personal_statement",
              "declaration_truthful", "declaration_signature_name"):
        assert f in fields, f


def test_submit_conditional_and_cross_field_rules(client, applicant, draft, reference):
    start = reference["intake_start"]
    data = complete_data(reference)
    data.update({
        "date_of_birth": (start - timedelta(days=14 * 365)).isoformat(),  # 14 at course start
        "passport_expiry_date": (start - timedelta(days=1)).isoformat(),
        "test_date": (start - timedelta(days=3 * 365)).isoformat(),
        "previous_visa_refusal": True,
        "funding_source": "sponsor",
        "been_to_uk": True,
        "declaration_signature_name": "Someone Else",
        "personal_statement": "Too short.",
    })
    del data["funder_details"], data["test_reference"]
    data["test_scores"] = {"overall": 6.5}
    assert put(client, applicant, draft["id"], data).status_code == 200
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    codes = {(d["field"], d["code"]) for d in r.json()["error"]["details"]}
    expected = {
        ("date_of_birth", "ERR_DOB_RANGE"), ("passport_expiry_date", "ERR_DATE_RANGE"),
        ("test_date", "ERR_DATE_RANGE"), ("refusal_details", "ERR_REQUIRED"), ("funder_details", "ERR_REQUIRED"),
        ("previous_uk_study", "ERR_REQUIRED"), ("declaration_signature_name", "ERR_SIGNATURE_MISMATCH"),
        ("personal_statement", "ERR_TOO_SHORT"), ("test_reference", "ERR_REQUIRED"),
        ("test_scores.reading", "ERR_REQUIRED"),
    }
    assert expected <= codes, expected - codes


def test_declarations_must_be_affirmative(client, applicant, draft, reference):
    data = complete_data(reference)
    data["declaration_truthful"] = False
    put(client, applicant, draft["id"], data)
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert ("declaration_truthful", "ERR_REQUIRED") in {(d["field"], d["code"]) for d in r.json()["error"]["details"]}


def test_unverified_email_blocks_submission(client, reference):
    a = register(client, "unverified@example.com", verify=False)
    d = client.post(f"{API}/applications", headers=a.headers).json()
    put(client, a, d["id"], complete_data(reference, email="unverified@example.com"))
    r = client.post(f"{API}/applications/{d['id']}/submit", headers=a.headers)
    assert r.status_code == 422
    assert [x["code"] for x in r.json()["error"]["details"]] == ["ERR_EMAIL_NOT_VERIFIED"]


def test_submission_check_endpoint(client, applicant, draft, reference):
    r = client.get(f"{API}/applications/{draft['id']}/submission-check", headers=applicant.headers)
    assert r.json()["ready"] is False and r.json()["problems"]
    put(client, applicant, draft["id"], complete_data(reference))
    r = client.get(f"{API}/applications/{draft['id']}/submission-check", headers=applicant.headers)
    assert r.json() == {"ready": True, "problems": [],
                        "outstanding_documents": ["passport", "qualification", "english", "financial"]}


def test_successful_submission(client, applicant, draft, reference, owner_engine):
    put(client, applicant, draft["id"], complete_data(reference))
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted" and body["editable"] is False
    assert body["data"]["submitted_at"] and body["data"]["declaration_at"]
    assert body["data"]["privacy_notice_version"] == "draft-2026-09"
    assert audit_actions(owner_engine, draft["id"])[-1] == "application.submit"
    confirmation = [m for m in notifications.OUTBOX if m.template_key == "application.submitted"]
    assert confirmation and draft["ref"] in confirmation[0].subject


@pytest.mark.priority("T-02")
def test_t02_edit_after_submission(client, applicant, draft, reference):
    """T-02: applicant edits application after submission -> 409 ERR_NOT_EDITABLE."""
    put(client, applicant, draft["id"], complete_data(reference))
    client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    r = put(client, applicant, draft["id"], {"nationality": "GB"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ERR_NOT_EDITABLE"


def test_submit_twice_invalid_state(client, applicant, draft, reference):
    put(client, applicant, draft["id"], complete_data(reference))
    client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"


def test_new_application_after_submission_returns_submitted_one(client, applicant, draft, reference):
    put(client, applicant, draft["id"], complete_data(reference))
    client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    r = client.post(f"{API}/applications", headers=applicant.headers)
    assert r.status_code == 200 and r.json()["id"] == draft["id"]
    mine = client.get(f"{API}/applications/mine", headers=applicant.headers).json()
    assert [m["status"] for m in mine] == ["submitted"]


# --- Access control (LLD 6, T-16) ---------------------------------------------------------------
@pytest.mark.priority("T-16")
def test_t16_other_applicant_gets_not_found(client, applicant, draft):
    """T-16: applicant requests another applicant's application -> 404, existence not revealed."""
    other = register(client, "other@example.com")
    for method, path in (("get", ""), ("put", ""), ("post", "/submit"), ("get", "/submission-check")):
        kwargs = {"json": {"data": {}}} if method == "put" else {}
        r = getattr(client, method)(f"{API}/applications/{draft['id']}{path}", headers=other.headers, **kwargs)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"]["code"] == "ERR_NOT_FOUND"
    missing = client.get(f"{API}/applications/999999", headers=other.headers)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_staff_can_read_but_not_write_applicant_data(client, applicant, draft, staff):
    s = staff("admissions")
    assert client.get(f"{API}/applications/{draft['id']}", headers=s.headers).status_code == 200
    assert put(client, s, draft["id"], {"nationality": "GB"}).status_code == 403
    assert client.post(f"{API}/applications/{draft['id']}/submit", headers=s.headers).status_code == 403


def test_applicant_cannot_use_staff_endpoints(client, applicant, draft):
    for path in ("/applications", f"/applications/{draft['id']}/audit", f"/applications/{draft['id']}/duplicates",
                 "/admin/users"):
        assert client.get(f"{API}{path}", headers=applicant.headers).status_code == 403, path


def test_staff_list_search_and_pagination(client, staff, reference):
    for i, name in enumerate(["Aisha Khan", "Bilal Ahmed", "Chen Wei"]):
        a = register(client, f"list{i}@example.com", name=name, verify=False)
        client.post(f"{API}/applications", headers=a.headers)
    s = staff("compliance")
    r = client.get(f"{API}/applications?page_size=2", headers=s.headers).json()
    assert r["total"] == 3 and len(r["items"]) == 2 and r["page_size"] == 2
    r = client.get(f"{API}/applications?q=bilal", headers=s.headers).json()
    assert [i["full_name"] for i in r["items"]] == ["Bilal Ahmed"]
    r = client.get(f"{API}/applications?status=submitted", headers=s.headers).json()
    assert r["total"] == 0


def test_audit_trail_chronological_and_role_restricted(client, applicant, draft, reference, staff):
    put(client, applicant, draft["id"], complete_data(reference))
    client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    s = staff("senior")
    trail = client.get(f"{API}/applications/{draft['id']}/audit", headers=s.headers).json()
    assert [e["action"] for e in trail] == ["application.create", "application.save", "application.submit"]
    assert trail[0]["user_name"] == "Priya Raman" and trail[0]["user_role"] == "applicant"
    assert [e["at"] for e in trail] == sorted(e["at"] for e in trail)
    finance = staff("finance")
    assert client.get(f"{API}/applications/{draft['id']}/audit", headers=finance.headers).status_code == 403


# --- 8.8 Duplicate detection (FR-15) ---------------------------------------------------------
def test_duplicate_detection(client, applicant, draft, reference, staff, owner_engine):
    put(client, applicant, draft["id"], complete_data(reference))
    twin = register(client, "twin@example.com", name="PRIYA  raman")
    d2 = client.post(f"{API}/applications", headers=twin.headers).json()
    data2 = complete_data(reference, full_name="PRIYA raman", email="twin@example.com")
    data2["passport_number"] = "Z 1234567"  # same passport, different spacing/case
    put(client, twin, d2["id"], data2)
    s = staff("admissions")
    dupes = client.get(f"{API}/applications/{d2['id']}/duplicates", headers=s.headers).json()
    assert len(dupes) == 1 and dupes[0]["id"] == draft["id"]
    assert set(dupes[0]["matched_on"]) == {"passport_number", "date_of_birth+full_name"}
    # Recorded on submission for review, never auto-merged
    client.post(f"{API}/applications/{d2['id']}/submit", headers=twin.headers)
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action='application.submit'")).scalar_one()
    assert "1 possible duplicate" in detail and draft["ref"] in detail


def test_duplicate_detection_excludes_rejected_and_self(client, applicant, draft, reference, staff):
    put(client, applicant, draft["id"], complete_data(reference))
    s = staff("admissions")
    assert client.get(f"{API}/applications/{draft['id']}/duplicates", headers=s.headers).json() == []


# --- Further submission rules --------------------------------------------------------------
def _submit_codes(client, actor, app_id):
    r = client.post(f"{API}/applications/{app_id}/submit", headers=actor.headers)
    assert r.status_code == 422, r.text
    return {(d["field"], d["code"]) for d in r.json()["error"]["details"]}


def test_nested_completeness(client, applicant, draft, reference):
    data = complete_data(reference)
    data["home_address"] = {"line1": "12 MG Road"}
    data["qualifications"] = [{"institution": "College", "start_date": "2021-01-01", "end_date": "2020-01-01"}]
    data["been_to_uk"] = True
    data["previous_uk_study"] = [{"institution": "Some College"}]
    data["declaration_data_consent"] = False
    put(client, applicant, draft["id"], data)
    codes = _submit_codes(client, applicant, draft["id"])
    for expected in [("home_address.city", "ERR_REQUIRED"), ("home_address.country", "ERR_REQUIRED"),
                     ("qualifications.0.grade", "ERR_REQUIRED"), ("qualifications.0.end_date", "ERR_DATE_RANGE"),
                     ("previous_uk_study.0.completed", "ERR_REQUIRED"),
                     ("declaration_data_consent", "ERR_REQUIRED")]:
        assert expected in codes, expected


def test_empty_previous_uk_study_confirms_none(client, applicant, draft, reference):
    data = complete_data(reference)
    data["been_to_uk"] = True
    data["previous_uk_study"] = []
    assert put(client, applicant, draft["id"], data).json()["data"]["previous_uk_study"] == []
    r = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert r.status_code == 200, r.text


def test_references_rechecked_at_submission(client, applicant, draft, reference, owner_engine):
    data = complete_data(reference)
    data["agent_id"] = reference["agent_id"]
    data["passport_issue_date"] = "2022-03-01"
    put(client, applicant, draft["id"], data)
    with owner_engine.connect() as conn:  # configuration changes after the draft was saved
        conn.execute(text("UPDATE programmes SET status='inactive'"))
        conn.execute(text("UPDATE agents SET status='suspended'"))
        conn.execute(text("UPDATE intakes SET application_deadline = current_date - 1 WHERE id=:i"),
                     {"i": reference["intake_id"]})
        conn.commit()
    codes = _submit_codes(client, applicant, draft["id"])
    assert {("programme_id", "ERR_INVALID_REFERENCE"), ("agent_id", "ERR_INVALID_REFERENCE"),
            ("intake_id", "ERR_DEADLINE_PASSED")} <= codes


def test_deadline_passed_on_save(client, applicant, draft, reference, owner_engine):
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE intakes SET application_deadline = current_date - 1 WHERE id=:i"),
                     {"i": reference["intake_id"]})
        conn.commit()
    r = put(client, applicant, draft["id"], {"intake_id": reference["intake_id"]})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "ERR_DEADLINE_PASSED"


def test_passport_expiry_before_issue(client, applicant, draft, reference):
    data = complete_data(reference)
    data["passport_issue_date"] = "2024-01-01"
    data["passport_expiry_date"] = "2023-01-01"
    put(client, applicant, draft["id"], data)
    assert ("passport_expiry_date", "ERR_DATE_RANGE") in _submit_codes(client, applicant, draft["id"])


def test_agent_attribution_saved(client, applicant, draft, reference):
    r = put(client, applicant, draft["id"], {"agent_id": reference["agent_id"]})
    assert r.json()["agent_id"] == reference["agent_id"]


def test_naive_if_match_accepted(client, applicant, draft):
    naive = draft["updated_at"].replace("Z", "").split("+")[0]
    assert put(client, applicant, draft["id"], {"nationality": "IN"}, etag=naive).status_code == 200


def test_bad_if_match(client, applicant, draft):
    r = put(client, applicant, draft["id"], {"nationality": "IN"}, etag="yesterday")
    assert r.status_code == 400
