"""The CAS assignment guard chain (LLD 7.3, 8.4, 8.5; FR-53 to FR-61).

These are release blockers: a failure here means a CAS could be issued without the controls that
the sponsor licence depends on. T-06 to T-11 are the LLD 18.2 priority cases.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, text

from app.config import get_settings
from tests.fixtures import (
    API,
    assign_cas,
    audit_actions,
    cas_body,
    do_academic,
    do_compliance,
    do_offer,
    pay,
    run_to_precas,
    submit_application,
)


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def ready(client, applicant, draft, reference, team):
    """An application at Pre-CAS Ready with both sign-offs, conditions met and the deposit paid."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    return draft


# --- T-06: role restriction (FR-53) ------------------------------------------------------------
@pytest.mark.priority("T-06")
@pytest.mark.parametrize("role", ["admissions", "compliance", "finance"])
def test_t06_only_senior_may_assign_cas(client, ready, reference, team, staff, role, owner_engine):
    """T-06: admissions user calls the CAS endpoint -> 403 ERR_ROLE_FORBIDDEN."""
    actor = team[role]
    r = assign_cas(client, actor, ready["id"], reference)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR_ROLE_FORBIDDEN"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cas")).scalar_one() == 0


def test_applicant_cannot_reach_the_cas_endpoint(client, applicant, ready, reference):
    assert assign_cas(client, applicant, ready["id"], reference).status_code == 403


# --- T-07 and T-08: segregation of duties (FR-54, FR-55, LLD 8.4) ------------------------------
@pytest.mark.priority("T-07")
def test_t07_senior_who_completed_compliance_cannot_assign(client, applicant, draft, reference, team, staff):
    """T-07: the senior who completed the compliance review assigns that CAS -> 422 ERR_SEGREGATION_OF_DUTIES."""
    senior = team["senior"]
    # The senior user performs the compliance review themselves, then tries to assign the CAS.
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    for condition in offer["conditions"]:
        client.patch(f"{API}/offers/{offer['id']}/conditions/{condition['id']}", headers=team["admissions"].headers,
                     json={"status": "met"})  # fmt: skip
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    pay(client, team["finance"], draft["id"])
    assert do_compliance(client, senior, draft["id"]).status_code == 200

    blocked = assign_cas(client, senior, draft["id"], reference)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "ERR_SEGREGATION_OF_DUTIES"
    assert "someone other than" in blocked.json()["error"]["message"]


def test_senior_who_completed_the_academic_assessment_cannot_assign(client, applicant, draft, reference, team):
    senior = team["senior"]
    submit_application(client, applicant, draft["id"], reference)
    assert do_academic(client, senior, draft["id"]).status_code == 200  # senior acts as admissions here
    offer = do_offer(client, senior, draft["id"]).json()
    for condition in offer["conditions"]:
        client.patch(f"{API}/offers/{offer['id']}/conditions/{condition['id']}", headers=senior.headers,
                     json={"status": "met"})  # fmt: skip
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    pay(client, team["finance"], draft["id"])
    do_compliance(client, team["compliance"], draft["id"])
    r = assign_cas(client, senior, draft["id"], reference)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_SEGREGATION_OF_DUTIES"


@pytest.mark.priority("T-08")
def test_t08_different_senior_assigns_the_cas(client, ready, reference, team, owner_engine):
    """T-08: a different senior assigns the CAS -> 201; cas row, student_record and audit entry all created."""
    r = assign_cas(client, team["senior"], ready["id"], reference)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["cas_number"] == "E4G0ABC12345"
    assert body["authorised_by"] == "Staff Senior"
    assert body["tuition_fee"] == "15000.00" and body["amount_paid"] == "3000.00"

    with owner_engine.connect() as conn:
        cas = conn.execute(text("SELECT id, application_id, level1_user, status FROM cas")).one()
        student = conn.execute(text("SELECT cas_id, enrolment_status FROM student_records")).one()
    assert cas.application_id == ready["id"] and cas.status == "assigned"
    assert student.cas_id == cas.id and student.enrolment_status == "awaiting_arrival"

    actions = audit_actions(owner_engine, ready["id"])
    assert actions[-1] == "cas.assign"
    detail = client.get(f"{API}/applications/{ready['id']}/audit", headers=team["senior"].headers).json()[-1]
    assert "E4G0ABC12345" in detail["detail"] and "Level 1 User" in detail["detail"]

    app = client.get(f"{API}/applications/{ready['id']}", headers=team["senior"].headers).json()
    assert app["status"] == "cas_assigned"
    assert app["student_record"]["enrolment_status"] == "awaiting_arrival"


def test_cas_assignment_is_recorded_against_the_allocation(client, ready, reference, team):
    assign_cas(client, team["senior"], ready["id"], reference)
    register = client.get(f"{API}/cas", headers=team["senior"].headers).json()
    assert register["total"] == 1
    assert register["items"][0]["application_ref"] == ready["ref"]
    assert register["allocation"] == {"period_label": "Test period", "period_start": register["allocation"]["period_start"],
                                      "period_end": register["allocation"]["period_end"], "allocated": 7,
                                      "assigned": 1, "remaining": 6}  # fmt: skip


# --- T-09: deposit gate (FR-69, LLD 8.5) -------------------------------------------------------
@pytest.mark.priority("T-09")
def test_t09_deposit_gate_and_override(client, applicant, draft, reference, team, owner_engine):
    """T-09: CAS assigned with deposit unpaid, gate enabled -> 422 ERR_DEPOSIT_NOT_MET;
    with an override reason, 201 plus an override audit entry."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    blocked = assign_cas(client, team["senior"], draft["id"], reference)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "ERR_DEPOSIT_NOT_MET"
    assert "3000" in blocked.json()["error"]["message"]

    allowed = assign_cas(client, team["senior"], draft["id"], reference,
                         deposit_override_reason="Sponsor letter held on file; deposit waived by the AO.")  # fmt: skip
    assert allowed.status_code == 201
    actions = audit_actions(owner_engine, draft["id"])
    assert "cas.deposit_override" in actions and actions[-1] == "cas.assign"


def test_partial_deposit_still_blocks(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    pay(client, team["finance"], draft["id"], "2999.99")
    r = assign_cas(client, team["senior"], draft["id"], reference)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_DEPOSIT_NOT_MET"


def test_deposit_gate_can_be_switched_off(client, applicant, draft, reference, team, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE system_settings SET value='false' WHERE key='DEPOSIT_GATE_ENABLED'"))
        conn.commit()
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201


def test_missing_fee_schedule_fails_closed(client, applicant, draft, reference, team, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text("DELETE FROM application_fees"))
        conn.execute(text("UPDATE fee_schedules SET effective_to = current_date"))
        conn.commit()
    r = assign_cas(client, team["senior"], draft["id"], reference)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_DEPOSIT_NOT_MET"
    assert "No fee schedule" in r.json()["error"]["message"]


# --- T-10: outstanding conditions (FR-40) ------------------------------------------------------
@pytest.mark.priority("T-10")
def test_t10_outstanding_condition_blocks_cas(client, applicant, draft, reference, team, owner_engine):
    """T-10: CAS assigned with an offer condition outstanding -> 422 ERR_CONDITIONS_OUTSTANDING."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], conditions_met=False)  # fmt: skip
    r = assign_cas(client, team["senior"], draft["id"], reference)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR_CONDITIONS_OUTSTANDING"
    assert "Pay the tuition fee deposit" in r.json()["error"]["message"]

    offer_id = client.get(f"{API}/applications/{draft['id']}",
                          headers=team["senior"].headers).json()["offer"]["id"]  # fmt: skip
    override = client.post(f"{API}/offers/{offer_id}/override", headers=team["senior"].headers,
                           json={"reason": "Evidence sighted in person by the Authorising Officer."})  # fmt: skip
    assert override.status_code == 200
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    assert "offer.override" in audit_actions(owner_engine, draft["id"])


def test_only_senior_may_override_conditions(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], conditions_met=False)  # fmt: skip
    offer_id = client.get(f"{API}/applications/{draft['id']}",
                          headers=team["senior"].headers).json()["offer"]["id"]  # fmt: skip
    r = client.post(f"{API}/offers/{offer_id}/override", headers=team["admissions"].headers,
                    json={"reason": "Trust me, the evidence exists somewhere."})  # fmt: skip
    assert r.status_code == 403


# --- Sign-offs, allocation and duplicates ------------------------------------------------------
def test_both_signoffs_required(client, applicant, draft, reference, team, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    with owner_engine.connect() as conn:  # simulate an assessment that was never signed off
        conn.execute(text("UPDATE academic_assessments SET completed_by = NULL, completed_at = NULL"))
        conn.commit()
    r = assign_cas(client, team["senior"], draft["id"], reference)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_SIGNOFF_INCOMPLETE"
    assert "academic assessment" in r.json()["error"]["message"]


def test_allocation_exceeded_needs_a_second_confirmation(client, applicant, draft, reference, team, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE cas_allocations SET allocated_count = 0"))
        conn.commit()
    warned = assign_cas(client, team["senior"], draft["id"], reference)
    assert warned.status_code == 422
    assert warned.json()["error"]["code"] == "ERR_ALLOCATION_EXCEEDED"
    assert warned.json()["error"]["details"][0]["remaining"] == 0

    confirmed = assign_cas(client, team["senior"], draft["id"], reference, allocation_override_confirmed=True)
    assert confirmed.status_code == 201
    assert "cas.allocation_warning" in audit_actions(owner_engine, draft["id"])


def test_cas_number_format_and_reuse(client, applicant, draft, reference, team, staff, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    bad = assign_cas(client, team["senior"], draft["id"], reference, cas_number="short")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "ERR_CAS_FORMAT"
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201

    from tests.fixtures import register

    other = register(client, "second@example.com", name="Second Applicant")
    app2 = client.post(f"{API}/applications", headers=other.headers).json()
    run_to_precas(client, other, app2["id"], reference, team["admissions"], team["compliance"], team["finance"])
    reused = assign_cas(client, team["senior"], app2["id"], reference)  # same CAS number
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "ERR_CAS_DUPLICATE"


@pytest.mark.priority("T-11")
def test_t11_concurrent_assignment(client, ready, reference, team):
    """T-11: two concurrent CAS assignments on one application -> one 201, one 409 ERR_CAS_DUPLICATE."""
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(2)

    def attempt(cas_number: str) -> None:
        barrier.wait(timeout=10)
        r = client.post(f"{API}/applications/{ready['id']}/cas", headers=team["senior"].headers,
                        json=cas_body(reference, cas_number=cas_number))  # fmt: skip
        results.append((r.status_code, r.json().get("error", {}).get("code", "")))

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("E4G0ABC12345", "E4G0ABC99999")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    codes = sorted(status for status, _ in results)
    assert codes == [201, 409], results
    assert [code for status, code in results if status == 409] == ["ERR_CAS_DUPLICATE"]

    # Exactly one CAS and one student record exist.
    engine = create_engine(get_settings().migration_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cas")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM student_records")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM audit_entries WHERE action='cas.assign'")).scalar_one() == 1
    engine.dispose()


def test_cas_cannot_be_assigned_before_precas_ready(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    r = assign_cas(client, team["senior"], draft["id"], reference)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"


def test_guard_failures_leave_no_trace(client, applicant, draft, reference, team, owner_engine):
    """Any guard failure leaves the application in its prior state with nothing written (LLD 7.3)."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], conditions_met=False, deposit=False)  # fmt: skip
    before = audit_actions(owner_engine, draft["id"])
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 422
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cas")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM student_records")).scalar_one() == 0
        status = conn.execute(text("SELECT status FROM applications WHERE id=:i"), {"i": draft["id"]}).scalar_one()
    assert status == "precas_ready"
    assert audit_actions(owner_engine, draft["id"]) == before


# --- Review follow-ups (independent review of Sprint 2) ----------------------------------------
def test_deposit_override_needs_a_substantive_reason(client, applicant, draft, reference, team):
    """The override is the only senior bypass of the deposit gate, so a token reason must not pass."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    r = assign_cas(client, team["senior"], draft["id"], reference, deposit_override_reason="x")
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"ERR_REASON_REQUIRED", "ERR_VALIDATION"}
    r = assign_cas(client, team["senior"], draft["id"], reference, deposit_override_reason="   ok   ")
    assert r.status_code == 422


def test_override_entry_shares_the_fate_of_the_assignment(client, applicant, draft, reference, team, owner_engine):
    """A deposit override recorded by a guard must roll back with the transition it authorised."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"], deposit=False)  # fmt: skip
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE cas_allocations SET allocated_count = 0"))
        conn.commit()
    before = audit_actions(owner_engine, draft["id"])
    blocked = assign_cas(client, team["senior"], draft["id"], reference,
                         deposit_override_reason="Sponsor letter held on file; deposit waived by the AO.")  # fmt: skip
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "ERR_ALLOCATION_EXCEEDED"
    # The override must not survive the failed assignment it was meant to authorise.
    assert audit_actions(owner_engine, draft["id"]) == before

    allowed = assign_cas(client, team["senior"], draft["id"], reference, allocation_override_confirmed=True,
                         deposit_override_reason="Sponsor letter held on file; deposit waived by the AO.")  # fmt: skip
    assert allowed.status_code == 201
    actions = audit_actions(owner_engine, draft["id"])
    assert actions.count("cas.deposit_override") == 1
    assert "cas.allocation_warning" in actions and actions[-1] == "cas.assign"


def test_precas_summary_blocks_the_reviewer_who_would_assign(client, applicant, draft, reference, team, staff):
    """FR-55 is visible before the CAS form: the summary tells the senior reviewer they cannot assign."""
    senior = staff("senior", "senior.reviewer@staff.test")
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], senior, team["finance"])
    mine = client.get(f"{API}/applications/{draft['id']}/precas", headers=senior.headers).json()
    assert mine["ready_for_cas"] is True  # the record is ready
    assert mine["assignment"]["can_assign"] is False  # but not by this user
    assert any("segregation of duties" in r for r in mine["assignment"]["reasons"])

    other = client.get(f"{API}/applications/{draft['id']}/precas", headers=team["senior"].headers).json()
    assert other["ready_for_cas"] is True and other["blockers"] == []
    assert other["assignment"]["can_assign"] is True


def test_precas_summary_reports_a_missing_allocation_period(client, ready, reference, team, owner_engine):
    with owner_engine.connect() as conn:
        conn.execute(text("UPDATE cas_allocations SET status = 'closed'"))
        conn.commit()
    summary = client.get(f"{API}/applications/{ready['id']}/precas", headers=team["senior"].headers).json()
    assert summary["ready_for_cas"] is False
    assert any("allocation period" in b for b in summary["blockers"])


def test_allocation_update_applies_the_whole_period(client, team, owner_engine):
    body = {"period_label": "2030/31", "period_start": "2030-01-01", "period_end": "2030-12-31",
            "allocated_count": 10}  # fmt: skip
    assert client.post(f"{API}/admin/allocations", json=body, headers=team["senior"].headers).status_code == 201
    corrected = {**body, "period_end": "2031-06-30", "allocated_count": 12}
    r = client.post(f"{API}/admin/allocations", json=corrected, headers=team["senior"].headers)
    assert r.status_code == 201
    assert r.json() == {"period_label": "2030/31", "period_start": "2030-01-01", "period_end": "2031-06-30",
                        "allocated": 12, "assigned": 0, "remaining": 12}  # fmt: skip
    with owner_engine.connect() as conn:
        end = conn.execute(text("SELECT period_end FROM cas_allocations WHERE period_label = '2030/31'")).scalar_one()
    assert str(end) == "2031-06-30"
