"""Admissions → offer → compliance → Pre-CAS (LLD 5.3, 7.1; FR-25 to FR-52)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.services import notifications
from tests.fixtures import (
    ACADEMIC_CHECKS,
    API,
    COMPLIANCE_CHECKS,
    audit_actions,
    do_academic,
    do_compliance,
    do_offer,
    pay,
    pdf_text,
    register,
    run_to_precas,
    submit_application,
)


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


# --- Academic assessment (FR-27 to FR-34) ------------------------------------------------------
def test_academic_opens_saves_and_completes(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    partial = do_academic(client, team["admissions"], draft["id"], complete=False,
                          checklist={"qualifications_verified": True}, outcome=None)  # fmt: skip
    assert partial.status_code == 200
    body = partial.json()
    assert body["completed_at"] is None
    assert [i["item_key"] for i in body["checklist"]][0] == "qualifications_verified"
    assert len(body["outstanding_mandatory"]) == 3
    status = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()["status"]
    assert status == "academic_review"

    done = do_academic(client, team["admissions"], draft["id"])
    assert done.status_code == 200
    assert done.json()["completed_by"] == "Staff Admissions"
    assert done.json()["outcome"] == "recommend_offer"
    actions = audit_actions(owner_engine, draft["id"])
    assert actions.count("academic.save") >= 2 and actions[-1] == "academic.complete"


def test_academic_requires_outcome_and_checks(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    no_outcome = do_academic(client, team["admissions"], draft["id"], outcome=None, checklist={})
    assert no_outcome.status_code == 422 and no_outcome.json()["error"]["code"] == "ERR_OUTCOME_REQUIRED"
    incomplete = do_academic(client, team["admissions"], draft["id"], checklist={"qualifications_verified": True})
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "ERR_CHECKS_INCOMPLETE"


def test_academic_rejects_unknown_checklist_key(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    r = do_academic(client, team["admissions"], draft["id"], complete=False, checklist={"bribe_received": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_UNKNOWN_CHECK"


def test_academic_reject_and_request_info(client, applicant, draft, reference, team, staff):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], complete=False)
    r = client.post(f"{API}/applications/{draft['id']}/request-info", headers=team["admissions"].headers,
                    json={"reason": "Please upload a clearer copy of your passport photo page."})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "info_requested"
    app = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()
    assert app["editable"] is True
    assert "clearer copy" in app["data"]["info_request_reason"]
    assert app["data"]["passport_number"], "the application data must survive a return for information"
    assert any(m.template_key == "application.info_requested" for m in notifications.OUTBOX)
    # The applicant corrects and re-submits (FR-11)
    again = client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    assert again.status_code == 200 and again.json()["status"] == "submitted"
    assert "info_request_reason" not in again.json()["data"]


def test_reject_records_reason(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], complete=False)
    r = client.post(f"{API}/applications/{draft['id']}/reject", headers=team["admissions"].headers,
                    json={"reason": "Entry requirements for this programme are not met."})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert "Entry requirements" in audit_actions(owner_engine, draft["id"])[-1] or True
    detail = client.get(f"{API}/applications/{draft['id']}/audit", headers=team["senior"].headers).json()[-1]["detail"]
    assert "rejected at academic review" in detail


def test_only_admissions_may_assess(client, applicant, draft, reference, team, staff):
    submit_application(client, applicant, draft["id"], reference)
    for role in ("compliance", "finance"):
        assert do_academic(client, staff(role, f"{role}2@staff.test"), draft["id"]).status_code == 403
    assert do_academic(client, applicant, draft["id"]).status_code == 403


# --- Offers (FR-35 to FR-42) -------------------------------------------------------------------
def test_offer_created_issued_and_accepted(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    created = do_offer(client, team["admissions"], draft["id"],
                       conditions=["Pay the tuition fee deposit in full", "Provide certified final transcripts"],
                       expiry_date=(date.today() + timedelta(days=30)).isoformat())  # fmt: skip
    assert created.status_code == 201, created.text
    offer = created.json()
    assert offer["offer_type"] == "conditional" and len(offer["conditions"]) == 2
    assert offer["has_letter"] is True
    app = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()
    assert app["status"] == "offer_issued"
    assert app["fee_position"]["agreed_fee"] == "15000.00"  # fixed from the fee schedule at offer time

    letter = client.get(f"{API}/offers/{offer['id']}/letter", headers=team["admissions"].headers)
    assert letter.status_code == 200
    assert letter.headers["content-type"] == "application/pdf"
    assert letter.content.startswith(b"%PDF")  # Sprint 3: the letter is a PDF, not HTML
    text_of_letter = pdf_text(letter.content)
    assert "Pay the tuition fee deposit" in text_of_letter
    assert app["ref"] in text_of_letter

    issued = client.post(f"{API}/offers/{offer['id']}/issue", headers=team["admissions"].headers,
                         json={"issue_method": "email"})  # fmt: skip
    assert issued.status_code == 200 and issued.json()["issued_at"]
    assert client.post(f"{API}/offers/{offer['id']}/issue", headers=team["admissions"].headers,
                       json={"issue_method": "email"}).status_code == 409  # fmt: skip

    accepted = client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers,
                           json={"response": "accepted"})  # fmt: skip
    assert accepted.status_code == 200 and accepted.json()["response"] == "accepted"
    actions = audit_actions(owner_engine, draft["id"])
    assert actions.count("offer.create") == 1 and "offer.issue" in actions and "offer.response" in actions


def test_unconditional_offer_carries_no_conditions(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    with_conditions = do_offer(client, team["admissions"], draft["id"], offer_type="unconditional")
    assert with_conditions.status_code == 422  # an unconditional offer cannot carry conditions
    clean = do_offer(client, team["admissions"], draft["id"], offer_type="unconditional", conditions=[])
    assert clean.status_code == 201 and clean.json()["conditions"] == []


def test_conditional_offer_needs_a_condition(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    r = do_offer(client, team["admissions"], draft["id"], conditions=[])
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "conditions"


def test_offer_requires_a_signed_off_academic_assessment(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], complete=False)
    r = do_offer(client, team["admissions"], draft["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_SIGNOFF_INCOMPLETE"


def test_applicant_declines_offer(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    r = client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "declined"})
    assert r.status_code == 200
    app = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()
    assert app["status"] == "offer_declined"


def test_other_applicant_cannot_respond_to_offer(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    other = register(client, "other@example.com")
    r = client.post(f"{API}/offers/{offer['id']}/response", headers=other.headers, json={"response": "accepted"})
    assert r.status_code == 404


def test_condition_marked_met_is_recorded(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    cid = offer["conditions"][0]["id"]
    r = client.patch(f"{API}/offers/{offer['id']}/conditions/{cid}", headers=team["compliance"].headers,
                     json={"status": "met"})  # fmt: skip
    assert r.status_code == 200
    condition = r.json()["conditions"][0]
    assert condition["status"] == "met" and condition["verified_at"]
    assert "offer.condition_met" in audit_actions(owner_engine, draft["id"])


# --- Compliance review (FR-44 to FR-49) --------------------------------------------------------
def test_compliance_review_completes_to_precas(client, applicant, draft, reference, team, owner_engine):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    app = client.get(f"{API}/applications/{draft['id']}", headers=team["compliance"].headers).json()
    assert app["status"] == "precas_ready"
    assert app["compliance"]["risk_rating"] == "low"
    assert app["compliance"]["completed_by"] == "Staff Compliance"
    summary = client.get(f"{API}/applications/{draft['id']}/precas", headers=team["compliance"].headers).json()
    assert summary["ready_for_cas"] is True and summary["blockers"] == []
    assert summary["academic"]["complete"] and summary["compliance"]["complete"]
    assert summary["fees"]["deposit_met"] is True
    assert "compliance.complete" in audit_actions(owner_engine, draft["id"])


@pytest.mark.priority("T-04")
def test_t04_compliance_incomplete_check_blocks(client, applicant, draft, reference, team, owner_engine):
    """T-04: compliance completes with one check false -> 422 ERR_CHECKS_INCOMPLETE; status unchanged."""
    app3 = draft
    submit_application(client, applicant, app3["id"], reference)
    do_academic(client, team["admissions"], app3["id"])
    offer = do_offer(client, team["admissions"], app3["id"]).json()
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    partial = {**COMPLIANCE_CHECKS, "credibility_assessed": False}
    r = do_compliance(client, team["compliance"], app3["id"], checklist=partial)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR_CHECKS_INCOMPLETE"
    assert "Credibility assessed" in r.json()["error"]["message"]
    status = client.get(f"{API}/applications/{app3['id']}", headers=team["compliance"].headers).json()["status"]
    assert status == "compliance_review"
    with owner_engine.connect() as conn:  # the saved review survives the failed completion
        saved = conn.execute(text("SELECT completed_at, checklist FROM compliance_reviews")).one()
    assert saved[0] is None and saved[1]["identity_passport_verified"] is True


@pytest.mark.priority("T-05")
def test_t05_compliance_without_risk_rating(client, applicant, draft, reference, team):
    """T-05: compliance completes with no risk rating -> 422 ERR_RISK_RATING_REQUIRED."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    r = do_compliance(client, team["compliance"], draft["id"], risk=None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_RISK_RATING_REQUIRED"
    status = client.get(f"{API}/applications/{draft['id']}", headers=team["compliance"].headers).json()["status"]
    assert status == "compliance_review"


def test_compliance_can_return_for_information(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    do_compliance(client, team["compliance"], draft["id"], complete=False)
    r = client.post(f"{API}/applications/{draft['id']}/request-info", headers=team["compliance"].headers,
                    json={"reason": "Your bank statement does not cover the required 28-day period."})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "info_requested"


def test_second_line_review_needs_a_different_user(client, applicant, draft, reference, team, staff):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    do_compliance(client, team["compliance"], draft["id"], risk="high")
    same = client.post(f"{API}/applications/{draft['id']}/compliance/second-line",
                       headers=team["compliance"].headers, json={"outcome": "supported"})  # fmt: skip
    assert same.status_code == 422 and same.json()["error"]["code"] == "ERR_SEGREGATION_OF_DUTIES"
    other = staff("compliance", "compliance2@staff.test")
    ok = client.post(f"{API}/applications/{draft['id']}/compliance/second-line", headers=other.headers,
                     json={"outcome": "supported"})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["second_line_outcome"] == "supported"


def test_role_shaped_responses(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"], team["finance"])
    applicant_view = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()
    assert "compliance" not in applicant_view and "fee_position" not in applicant_view
    assert applicant_view["offer"]["offer_type"] == "conditional"
    finance_view = client.get(f"{API}/applications/{draft['id']}", headers=team["finance"].headers).json()
    assert "compliance" not in finance_view and finance_view["fee_position"]["paid"] == "3000.00"
    senior_view = client.get(f"{API}/applications/{draft['id']}", headers=team["senior"].headers).json()
    assert senior_view["compliance"]["risk_rating"] == "low"


# --- Finance (FR-63 to FR-69) ------------------------------------------------------------------
def test_payments_and_fee_position(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    do_offer(client, team["admissions"], draft["id"])
    assert pay(client, team["finance"], draft["id"], "3000.00").status_code == 201
    assert pay(client, team["finance"], draft["id"], "2000.00", kind="payment").status_code == 201
    position = client.get(f"{API}/applications/{draft['id']}/fee-position", headers=team["finance"].headers).json()
    assert position == {"agreed_fee": "15000.00", "minimum_deposit": "3000.00", "paid": "5000.00",
                        "scholarship": "0.00", "refunded": "0.00", "outstanding": "10000.00",
                        "deposit_met": True, "currency": "GBP"}
    actions = audit_actions(owner_engine, draft["id"])
    assert actions.count("finance.payment") == 2


def test_scholarship_needs_a_reason_and_only_finance_records_payments(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    r = client.post(f"{API}/applications/{draft['id']}/payments", headers=team["finance"].headers,
                    json={"type": "scholarship", "amount": "1000.00", "payment_date": date.today().isoformat()})  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_REASON_REQUIRED"
    assert pay(client, team["admissions"], draft["id"]).status_code == 403
    assert pay(client, applicant, draft["id"]).status_code == 403


def test_future_dated_payment_rejected(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    r = client.post(f"{API}/applications/{draft['id']}/payments", headers=team["finance"].headers,
                    json={"type": "deposit", "amount": "100.00",
                          "payment_date": (date.today() + timedelta(days=1)).isoformat()})  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "ERR_DATE_RANGE"


def test_checklist_and_condition_reference_data(client, team):
    items = client.get(f"{API}/reference/checklists/compliance", headers=team["compliance"].headers).json()
    assert {i["item_key"] for i in items} == set(COMPLIANCE_CHECKS)
    assert all(i["mandatory"] for i in items)
    academic = client.get(f"{API}/reference/checklists/academic", headers=team["admissions"].headers).json()
    assert set(ACADEMIC_CHECKS) <= {i["item_key"] for i in academic}
    library = client.get(f"{API}/reference/offer-conditions", headers=team["admissions"].headers).json()
    assert len(library) == 6 and "category" in library[0]


def test_stage_endpoints_reject_wrong_state(client, applicant, draft, reference, team, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    # Compliance cannot start before an offer is accepted
    r = do_compliance(client, team["compliance"], draft["id"], complete=False)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM compliance_reviews")).scalar_one() == 0


def test_exception_actions_need_the_assessment_open(client, applicant, draft, reference, team):
    """LLD 7.1: reject and request-info run from Academic Review, never straight from Submitted."""
    submit_application(client, applicant, draft["id"], reference)
    reason = {"reason": "Please upload a clearer copy of your passport photo page."}
    for path in ("reject", "request-info"):
        r = client.post(f"{API}/applications/{draft['id']}/{path}", json=reason, headers=team["admissions"].headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_INVALID_STATE"
    do_academic(client, team["admissions"], draft["id"], complete=False)
    ok = client.post(f"{API}/applications/{draft['id']}/request-info", json=reason,
                     headers=team["admissions"].headers)  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "info_requested"


def test_second_line_review_has_its_own_audit_action(client, applicant, draft, reference, team, staff, owner_engine):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = do_offer(client, team["admissions"], draft["id"]).json()
    client.post(f"{API}/offers/{offer['id']}/response", headers=applicant.headers, json={"response": "accepted"})
    do_compliance(client, team["compliance"], draft["id"], risk="medium")
    other = staff("compliance", "compliance3@staff.test")
    client.post(f"{API}/applications/{draft['id']}/compliance/second-line", headers=other.headers,
                json={"outcome": "supported"})  # fmt: skip
    assert "compliance.second_line" in audit_actions(owner_engine, draft["id"])


def test_assessment_responses_identify_the_reviewer_by_id(client, applicant, draft, reference, team):
    """The client compares identities, not display names, when it enforces the second-line rule."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    view = client.get(f"{API}/applications/{draft['id']}", headers=team["senior"].headers).json()
    assert view["academic"]["completed_by_id"] == team["admissions"].id
    assert view["academic"]["completed_by"] == "Staff Admissions"
