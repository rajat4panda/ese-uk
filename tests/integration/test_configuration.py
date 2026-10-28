"""Configuration without code changes (BRD FR-105), every change audited.

The point of the screen is that nobody has to change a compliance threshold with SQL any more, so
the properties under test are: only the right roles can change it, a change takes effect straight
away, bad values are refused with a field error, and every change leaves the old and new value in
the audit trail.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.fixtures import API


@pytest.fixture
def team(staff):
    return {r: staff(r) for r in ("admissions", "compliance", "finance", "senior", "admin")}


def config_audit(owner_engine: Engine, action: str = "admin.config_change") -> list[str]:
    with owner_engine.connect() as conn:
        return list(conn.execute(text("SELECT detail FROM audit_entries WHERE action = :a ORDER BY at, id"),
                                 {"a": action}).scalars())  # fmt: skip


def setting(client, actor, key):
    return next(s for s in client.get(f"{API}/admin/settings", headers=actor.headers).json() if s["key"] == key)


# --- Settings --------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["admissions", "compliance", "finance"])
def test_only_senior_and_admin_change_settings(client, reference, team, role):
    assert client.get(f"{API}/admin/settings", headers=team[role].headers).status_code == 403
    res = client.put(f"{API}/admin/settings/ATTENDANCE_THRESHOLD_PCT", json={"value": "70"}, headers=team[role].headers)
    assert res.status_code == 403


def test_settings_are_listed_with_current_values(client, reference, team):
    rows = {s["key"]: s for s in client.get(f"{API}/admin/settings", headers=team["senior"].headers).json()}
    assert rows["ATTENDANCE_THRESHOLD_PCT"]["value"] == "80"
    assert rows["ATTENDANCE_THRESHOLD_PCT"]["unit"] == "%"
    assert "CAS_ALLOCATION" not in rows and "MAX_UPLOAD_BYTES" not in rows  # managed elsewhere


def test_a_change_is_audited_and_takes_effect(client, reference, team, owner_engine):
    res = client.put(f"{API}/admin/settings/ATTENDANCE_THRESHOLD_PCT", json={"value": "75"}, headers=team["admin"].headers)
    assert res.status_code == 200, res.text
    assert res.json()["value"] == "75" and res.json()["updated_by"]
    assert config_audit(owner_engine)[-1] == "ATTENDANCE_THRESHOLD_PCT (Attendance threshold): 80 -> 75"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT value FROM system_settings WHERE key = 'ATTENDANCE_THRESHOLD_PCT'")).scalar() == "75"


@pytest.mark.parametrize("key,value,field_code", [
    ("ATTENDANCE_THRESHOLD_PCT", "150", "ERR_OUT_OF_RANGE"),
    ("ATTENDANCE_THRESHOLD_PCT", "7.5", "ERR_INVALID_FORMAT"),
    ("ATTENDANCE_THRESHOLD_PCT", "eighty", "ERR_INVALID_FORMAT"),
    ("REPORTING_WORKING_DAYS", "11", "ERR_OUT_OF_RANGE"),  # the statutory duty is 10
    ("BCA_REFUSAL_MAX_PCT", "5.25", "ERR_INVALID_FORMAT"),
    ("BCA_REFUSAL_MAX_PCT", "NaN", "ERR_INVALID_FORMAT"),
    ("DEPOSIT_GATE_ENABLED", "maybe", "ERR_INVALID_FORMAT"),
    ("COMPLIANCE_INBOX", "not-an-email", "ERR_INVALID_FORMAT"),
])  # fmt: skip
def test_bad_values_are_refused(client, reference, team, key, value, field_code, owner_engine):
    res = client.put(f"{API}/admin/settings/{key}", json={"value": value}, headers=team["senior"].headers)
    assert res.status_code == 422, res.text
    detail = res.json()["error"]["details"][0]
    assert detail["field"] == "value" and detail["code"] == field_code
    assert config_audit(owner_engine) == []


def test_unknown_and_protected_keys_cannot_be_set(client, reference, team):
    for key in ("MAX_UPLOAD_BYTES", "CAS_ALLOCATION", "NOT_A_SETTING"):
        assert client.put(f"{API}/admin/settings/{key}", json={"value": "1"}, headers=team["admin"].headers).status_code == 404


def test_setting_the_same_value_is_refused(client, reference, team):
    res = client.put(f"{API}/admin/settings/ATTENDANCE_THRESHOLD_PCT", json={"value": "80"}, headers=team["admin"].headers)
    assert res.status_code == 409


def test_a_setting_without_a_row_is_created_on_first_change(client, reference, team, owner_engine):
    assert setting(client, team["admin"], "COMPLIANCE_INBOX")["is_default"] is True
    res = client.put(f"{API}/admin/settings/COMPLIANCE_INBOX", json={"value": "Visa.Compliance@ESE.example"},
                     headers=team["admin"].headers)  # fmt: skip
    assert res.status_code == 200 and res.json()["value"] == "visa.compliance@ese.example"
    assert res.json()["is_default"] is False
    assert config_audit(owner_engine)[-1].endswith("compliance@ese.example -> visa.compliance@ese.example")
    again = client.put(f"{API}/admin/settings/COMPLIANCE_INBOX", json={"value": "VISA.compliance@ese.example"},
                       headers=team["admin"].headers)  # fmt: skip
    assert again.status_code == 409  # the same address in different case is not a change


def test_numbers_are_stored_in_one_form(client, reference, team):
    assert client.put(f"{API}/admin/settings/ATTENDANCE_THRESHOLD_PCT", json={"value": "7.5e1"},
                      headers=team["admin"].headers).json()["value"] == "75"  # fmt: skip
    assert client.put(f"{API}/admin/settings/BCA_REFUSAL_MAX_PCT", json={"value": "4"},
                      headers=team["admin"].headers).json()["value"] == "4.0"  # fmt: skip
    client.put(f"{API}/admin/settings/BCA_REFUSAL_MAX_PCT", json={"value": "5"}, headers=team["admin"].headers)
    assert client.put(f"{API}/admin/settings/BCA_REFUSAL_MAX_PCT", json={"value": "5.00"},
                      headers=team["admin"].headers).status_code == 409  # fmt: skip


def test_the_deadline_warning_must_fall_inside_the_deadline(client, reference, team):
    res = client.put(f"{API}/admin/settings/ESCALATION_DAYS_BEFORE", json={"value": "9"}, headers=team["senior"].headers)
    assert res.status_code == 200  # 9 < 10
    res = client.put(f"{API}/admin/settings/REPORTING_WORKING_DAYS", json={"value": "8"}, headers=team["senior"].headers)
    assert res.status_code == 422 and res.json()["error"]["details"][0]["code"] == "ERR_OUT_OF_RANGE"


def test_only_the_authorising_officer_switches_off_the_deposit_gate(client, reference, team, owner_engine):
    res = client.put(f"{API}/admin/settings/DEPOSIT_GATE_ENABLED", json={"value": "false"}, headers=team["admin"].headers)
    assert res.status_code == 403
    assert config_audit(owner_engine) == []
    assert setting(client, team["admin"], "DEPOSIT_GATE_ENABLED")["ao_only"] is True
    res = client.put(f"{API}/admin/settings/DEPOSIT_GATE_ENABLED", json={"value": "false"}, headers=team["senior"].headers)
    assert res.status_code == 200 and res.json()["value"] == "false"


# --- Programmes and intakes --------------------------------------------------------------------------
def test_programme_added_edited_and_withdrawn_with_audit(client, reference, team, owner_engine):
    res = client.post(f"{API}/admin/programmes", json={"title": "MBA Entrepreneurship and Leadership",
                                                      "qualification_level": "RQF 7", "duration_months": 12},
                      headers=team["admin"].headers)  # fmt: skip
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    titles = [p["title"] for p in client.get(f"{API}/reference/programmes", headers=team["admissions"].headers).json()]
    assert "MBA Entrepreneurship and Leadership" in titles  # offered straight away
    client.patch(f"{API}/admin/programmes/{pid}", json={"duration_months": 18}, headers=team["admin"].headers)
    client.patch(f"{API}/admin/programmes/{pid}", json={"status": "inactive"}, headers=team["admin"].headers)
    titles = [p["title"] for p in client.get(f"{API}/reference/programmes", headers=team["admissions"].headers).json()]
    assert "MBA Entrepreneurship and Leadership" not in titles
    trail = config_audit(owner_engine)
    assert trail[-3].startswith("Programme added: MBA Entrepreneurship and Leadership")
    assert trail[-2] == f"Programme {pid}: duration_months: 12 -> 18"
    assert trail[-1] == f"Programme {pid}: status: active -> inactive"


def test_intake_deadline_after_start_is_refused(client, reference, team):
    start = date.today() + timedelta(days=100)
    res = client.post(f"{API}/admin/intakes", json={"label": "January 2028", "start_date": start.isoformat(),
                                                   "application_deadline": (start + timedelta(days=1)).isoformat()},
                      headers=team["senior"].headers)  # fmt: skip
    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == "application_deadline"


def test_intake_edit_checks_against_the_stored_date(client, reference, team):
    start = date.today() + timedelta(days=100)
    iid = client.post(f"{API}/admin/intakes", json={"label": "January 2028", "start_date": start.isoformat(),
                                                   "application_deadline": (start - timedelta(days=30)).isoformat()},
                      headers=team["senior"].headers).json()["id"]  # fmt: skip
    res = client.patch(f"{API}/admin/intakes/{iid}", json={"application_deadline": (start + timedelta(days=5)).isoformat()},
                       headers=team["senior"].headers)  # fmt: skip
    assert res.status_code == 422


# --- Fees --------------------------------------------------------------------------------------------
def test_finance_sets_fees_and_the_old_schedule_is_kept(client, reference, team, owner_engine):
    pid, iid = reference["programme_id"], reference["intake_id"]
    first = client.post(f"{API}/admin/fees", json={"programme_id": pid, "intake_id": iid, "tuition_fee": "16500.00",
                                                  "minimum_deposit": "4000.00"}, headers=team["finance"].headers)  # fmt: skip
    assert first.status_code == 201, first.text
    second = client.post(f"{API}/admin/fees", json={"programme_id": pid, "intake_id": iid, "tuition_fee": "17000.00",
                                                   "minimum_deposit": "4000.00"}, headers=team["finance"].headers)  # fmt: skip
    assert second.json()["tuition_fee"] == "17000.00"
    with owner_engine.connect() as conn:
        rows = conn.execute(text("SELECT tuition_fee, effective_to FROM fee_schedules WHERE programme_id = :p "
                                 "AND intake_id = :i ORDER BY id"), {"p": pid, "i": iid}).all()  # fmt: skip
    assert [str(r[0]) for r in rows][-2:] == ["16500.00", "17000.00"]
    assert rows[-2][1] is not None and rows[-1][1] is None  # the old one closed, not overwritten
    assert "-> 17000.00 (deposit 4000.00)" in config_audit(owner_engine, "finance.schedule_change")[-1]


@pytest.mark.parametrize("fee,deposit", [("0", "0"), ("1000", "2000"), ("1000", "-1")])
def test_bad_fees_are_refused(client, reference, team, fee, deposit):
    res = client.post(f"{API}/admin/fees", json={"programme_id": reference["programme_id"], "intake_id": reference["intake_id"],
                                                "tuition_fee": fee, "minimum_deposit": deposit},
                      headers=team["finance"].headers)  # fmt: skip
    assert res.status_code == 422


def test_admissions_cannot_set_fees(client, reference, team):
    res = client.post(f"{API}/admin/fees", json={"programme_id": reference["programme_id"], "intake_id": reference["intake_id"],
                                                "tuition_fee": "1000", "minimum_deposit": "0"},
                      headers=team["admissions"].headers)  # fmt: skip
    assert res.status_code == 403


def test_fees_cannot_be_set_for_a_closed_intake(client, reference, team):
    res = client.post(f"{API}/admin/fees", json={"programme_id": reference["programme_id"],
                                                "intake_id": reference["closed_intake_id"],
                                                "tuition_fee": "1000", "minimum_deposit": "0"},
                      headers=team["finance"].headers)  # fmt: skip
    assert res.status_code == 409


def test_allocations_are_listed_with_use(client, reference, team):
    client.post(f"{API}/admin/allocations", json={"period_label": "2026/27", "period_start": "2026-09-01",
                                                 "period_end": "2027-08-31", "allocated_count": 7},
                headers=team["senior"].headers)  # fmt: skip
    rows = client.get(f"{API}/admin/allocations", headers=team["senior"].headers).json()
    assert any(r["period_label"] == "2026/27" and r["allocated"] == 7 for r in rows)
