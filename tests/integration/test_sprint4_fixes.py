"""The Phase 1 Must gaps and the defects found by the requirements assessment of 26 September.

Each test names the requirement or the defect it closes. Several of these are regression tests in
the strict sense — they fail against the code as it was, which is the only way to know a defect
was actually fixed rather than described.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.services import notifications
from app.workers.jobs.compliance import engagement_check
from tests.fixtures import (
    API,
    EICAR,
    assign_cas,
    attend,
    do_academic,
    enrol,
    pdf_text,
    run_to_precas,
    submit_application,
)


@pytest.fixture
def session(owner_engine):
    from app.database import get_sessionmaker

    db = get_sessionmaker()()
    yield db
    db.close()


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior"), "admin": staff("admin")}  # fmt: skip


def upload(client, actor, app_id, *, category="passport", name="passport.pdf"):
    return client.post(
        f"{API}/applications/{app_id}/documents",
        headers=actor.headers,
        data={"category": category},
        files={"file": (name, b"%PDF-1.4\n" + b"x" * 400, "application/pdf")},
    )


# --- FR-24 / Defect 5: verifying a document ----------------------------------------------------
def test_a_document_can_be_marked_verified_and_the_verifier_is_recorded(client, applicant, draft, team):
    doc = upload(client, applicant, draft["id"]).json()
    assert doc["verified_at"] is None

    res = client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers,
                      json={"verified": True})  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["verified_at"] is not None

    detail = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()
    row = next(d for d in detail["documents"] if d["id"] == doc["id"])
    assert row["verified_by_name"] == "Admissions Staff" or row["verified_by_name"]


def test_verification_can_be_taken_back_and_both_directions_are_audited(client, applicant, draft, team):
    doc = upload(client, applicant, draft["id"]).json()
    client.post(f"{API}/documents/{doc['id']}/verify", headers=team["compliance"].headers, json={"verified": True})
    res = client.post(f"{API}/documents/{doc['id']}/verify", headers=team["compliance"].headers,
                      json={"verified": False})  # fmt: skip
    assert res.status_code == 200
    assert res.json()["verified_at"] is None

    audit = client.get(f"{API}/applications/{draft['id']}/audit", headers=team["compliance"].headers).json()
    verifies = [a for a in audit if a["action"] == "document.verify"]
    assert len(verifies) == 2
    assert "Verification removed" in verifies[-1]["detail"]


def test_verifying_twice_is_refused_rather_than_silently_ignored(client, applicant, draft, team):
    doc = upload(client, applicant, draft["id"]).json()
    client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers, json={"verified": True})
    again = client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers,
                        json={"verified": True})  # fmt: skip
    assert again.status_code == 409


def test_finance_and_the_applicant_cannot_verify_a_document(client, applicant, draft, team):
    """LLD 6: verification is a statement about evidence, and finance has no sight of evidence."""
    doc = upload(client, applicant, draft["id"]).json()
    for actor in (team["finance"], applicant):
        res = client.post(f"{API}/documents/{doc['id']}/verify", headers=actor.headers, json={"verified": True})
        assert res.status_code == 403


def test_the_evidence_pack_names_who_verified_each_document(
    client, applicant, draft, reference, team
):
    """FR-24 mattered because of this file: every document in it used to read 'Not verified'."""
    import zipfile

    doc = upload(client, applicant, draft["id"]).json()
    client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers, json={"verified": True})
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)

    job = client.post(f"{API}/applications/{draft['id']}/evidence-pack", headers=team["senior"].headers).json()
    download = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    index = pdf_text(archive.read(next(n for n in archive.namelist() if n.endswith("index.pdf"))))
    assert "Not verified" not in index
    # The name appears once for the academic sign-off regardless; a second occurrence is the
    # document verifier, which is what FR-24 added.
    assert index.count("Staff Admissions") >= 2


# --- Defect 2 / FR-08: a closed application must show itself ------------------------------------
def test_a_rejected_application_is_still_listed_for_its_applicant(client, applicant, draft, reference, team):
    """The applicant screens read this list, so it must not hide a closed record."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], outcome="reject", comments="Entry requirements not met.")

    mine = client.get(f"{API}/applications/mine", headers=applicant.headers).json()
    assert [a["status"] for a in mine] == ["rejected"]
    assert mine[0]["ref"] == draft["ref"]


def test_reading_my_application_after_a_rejection_does_not_mint_a_new_one(
    client, applicant, draft, reference, team, session
):
    """The defect itself. The applicant screens used to load by POSTing /applications, which
    returns the *open* application and creates a draft when there is none — so a rejected
    applicant was shown a blank form and silently consumed the next reference in the sequence.
    This asserts the read path the screens now use changes nothing."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], outcome="reject", comments="Entry requirements not met.")

    def counts() -> tuple[int, int]:
        apps = session.execute(
            text("SELECT count(*) FROM applications WHERE applicant_id = :a"), {"a": applicant.id}
        ).scalar_one()
        created = session.execute(
            text("SELECT count(*) FROM audit_entries WHERE action = 'application.create'")
        ).scalar_one()
        return apps, created

    session.commit()
    before = counts()
    mine = client.get(f"{API}/applications/mine", headers=applicant.headers).json()
    client.get(f"{API}/applications/{mine[0]['id']}", headers=applicant.headers)
    session.commit()
    assert counts() == before
    assert mine[0]["ref"] == draft["ref"]


# --- Defect 1: the offer and CAS notifications ---------------------------------------------------
def test_issuing_an_offer_emails_the_applicant(client, applicant, draft, reference, team):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    offer = client.post(f"{API}/applications/{draft['id']}/offer", headers=team["admissions"].headers,
                        json={"offer_type": "conditional", "conditions": ["Pay the deposit"]}).json()  # fmt: skip
    notifications.OUTBOX.clear()
    client.post(f"{API}/offers/{offer['id']}/issue", headers=team["admissions"].headers,
                json={"issue_method": "email"})  # fmt: skip

    sent = [m for m in notifications.OUTBOX if m.template_key == "offer.issued"]
    assert len(sent) == 1
    assert sent[0].recipient == applicant.email
    assert draft["ref"] in sent[0].subject
    assert "Pay the deposit" in sent[0].body


def test_assigning_a_cas_emails_the_applicant_the_number(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    notifications.OUTBOX.clear()
    assign_cas(client, team["senior"], draft["id"], reference)

    sent = [m for m in notifications.OUTBOX if m.template_key == "cas.assigned"]
    assert len(sent) == 1
    assert sent[0].recipient == applicant.email
    assert "E4G0ABC12345" in sent[0].body


# --- Defect 4: the engagement alert must not repeat every day -------------------------------------
def test_an_unresolved_attendance_breach_does_not_alert_again_the_next_day(
    client, applicant, draft, reference, team, session
):
    """The dedupe key used to carry the lookback window's start date, which moves daily, so a
    student below the threshold emailed the compliance inbox every morning indefinitely."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)
    student = client.get(f"{API}/applications/{draft['id']}/student", headers=team["compliance"].headers).json()
    enrol(client, team["compliance"], student["id"])
    for i, status in enumerate(["absent", "absent", "present"]):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"M{i}", status=status)  # fmt: skip

    assert engagement_check(session) == 1
    session.commit()
    # The day after, and the day after that: the breach is unresolved, and stays quiet.
    assert engagement_check(session, today=date.today() + timedelta(days=1)) == 0
    session.commit()
    assert engagement_check(session, today=date.today() + timedelta(days=2)) == 0
    session.commit()
    # Past the hold-off, it speaks again — an unresolved breach should not be forgotten either.
    assert engagement_check(session, today=date.today() + timedelta(days=15)) == 1


# --- FR-52: the CAS carries the figures the sponsor declared --------------------------------------
def test_the_cas_stores_the_figures_entered_not_the_ledger_balance(
    client, applicant, draft, reference, team
):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    res = assign_cas(client, team["senior"], draft["id"], reference,
                     tuition_fee="14250.00", amount_paid="4000.00")  # fmt: skip
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["tuition_fee"] == "14250.00"
    assert body["amount_paid"] == "4000.00"


def test_a_cas_cannot_declare_more_paid_than_the_fee(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    res = assign_cas(client, team["senior"], draft["id"], reference,
                     tuition_fee="1000.00", amount_paid="2000.00")  # fmt: skip
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ERR_VALIDATION"


def test_a_cas_cannot_declare_a_zero_tuition_fee(client, applicant, draft, reference, team):
    """A zero fee is a data-entry slip, and the old code produced one silently whenever no fee
    schedule matched."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    res = assign_cas(client, team["senior"], draft["id"], reference, tuition_fee="0.00", amount_paid="0.00")
    assert res.status_code == 422


# --- FR-55: the CAS register leaves the system ----------------------------------------------------
def test_the_cas_register_exports_as_csv_and_the_export_is_audited(
    client, applicant, draft, reference, team, session
):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)

    res = client.get(f"{API}/cas/export", headers=team["compliance"].headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[0][0] == "CAS number"
    assert len(rows) == 2
    assert rows[1][0] == "E4G0ABC12345"
    assert rows[1][1] == draft["ref"]

    actions = session.execute(text("SELECT action, detail FROM audit_entries WHERE action = 'cas.export'")).all()
    assert len(actions) == 1
    assert "1 row(s)" in actions[0][1]


def test_the_export_is_not_open_to_an_applicant(client, applicant):
    assert client.get(f"{API}/cas/export", headers=applicant.headers).status_code == 403


# --- FR-36: the English language evidence ----------------------------------------------------------
def test_completing_an_assessment_records_which_english_evidence_was_accepted(
    client, applicant, draft, reference, team
):
    submit_application(client, applicant, draft["id"], reference)
    res = do_academic(client, team["admissions"], draft["id"],
                      english_evidence="degree_taught_in_english", english_standard="CEFR B2")  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["english_evidence"] == "degree_taught_in_english"
    assert res.json()["english_standard"] == "CEFR B2"


def test_the_english_check_cannot_be_ticked_with_nothing_behind_it(
    client, applicant, draft, reference, team
):
    """FR-36: a tick says somebody looked. It does not say what they accepted, and a compliance
    visit asks the second question."""
    submit_application(client, applicant, draft["id"], reference)
    res = do_academic(client, team["admissions"], draft["id"], english_evidence=None, english_standard=None)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ERR_ENGLISH_EVIDENCE_REQUIRED"


def test_the_evidence_pack_repeats_the_english_evidence(client, applicant, draft, reference, team, session):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    detail = client.get(f"{API}/applications/{draft['id']}", headers=team["senior"].headers).json()
    assert detail["academic"]["english_evidence"] == "slt_test"
    assert detail["academic"]["english_standard"] == "IELTS UKVI 6.0"


# --- FR-100: correcting a staff account -------------------------------------------------------------
def test_an_administrator_can_correct_a_name_and_an_email(client, team, session):
    created = client.post(f"{API}/admin/users", headers=team["admin"].headers, json={
        "full_name": "Kristin Renee Sharp", "email": "k.sharp@example.com", "role": "senior",
    }).json()  # fmt: skip

    res = client.patch(f"{API}/admin/users/{created['id']}", headers=team["admin"].headers, json={
        "full_name": "Kristin Renee Sharpe", "email": "k.sharpe@example.com",
    })  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["full_name"] == "Kristin Renee Sharpe"
    assert res.json()["email"] == "k.sharpe@example.com"

    details = session.execute(
        text("SELECT detail FROM audit_entries WHERE action = 'admin.user_update' ORDER BY id")
    ).scalars().all()
    assert any("'Kristin Renee Sharp' -> 'Kristin Renee Sharpe'" in d for d in details)
    assert any("k.sharp@example.com' -> 'k.sharpe@example.com'" in d for d in details)


def test_a_correction_cannot_take_an_address_another_account_already_uses(client, team):
    created = client.post(f"{API}/admin/users", headers=team["admin"].headers, json={
        "full_name": "First Person", "email": "first@example.com", "role": "admissions",
    })  # fmt: skip
    assert created.status_code == 201, created.text
    a = created.json()
    client.post(f"{API}/admin/users", headers=team["admin"].headers, json={
        "full_name": "Second Person", "email": "second@example.com", "role": "admissions",
    })  # fmt: skip

    res = client.patch(f"{API}/admin/users/{a['id']}", headers=team["admin"].headers,
                       json={"email": "second@example.com"})  # fmt: skip
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ERR_EMAIL_IN_USE"


def test_only_an_administrator_may_correct_an_account(client, team):
    created = client.post(f"{API}/admin/users", headers=team["admin"].headers, json={
        "full_name": "Someone Else", "email": "someone@example.com", "role": "compliance",
    }).json()  # fmt: skip
    for actor in (team["senior"], team["compliance"]):
        res = client.patch(f"{API}/admin/users/{created['id']}", headers=actor.headers,
                           json={"full_name": "Changed Name"})  # fmt: skip
        assert res.status_code == 403


# --- FR-35: the application moves on when the assessment is signed off ------------------------------
def test_completing_the_academic_assessment_moves_the_application_on(
    client, applicant, draft, reference, team
):
    """It used to sit in Academic Review until somebody built an offer, so the pipeline could not
    tell an application nobody had looked at from one approved and waiting for its offer letter."""
    submit_application(client, applicant, draft["id"], reference)
    res = do_academic(client, team["admissions"], draft["id"])
    assert res.status_code == 200, res.text

    detail = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()
    assert detail["status"] == "offer_pending"


def test_an_unfinished_assessment_still_blocks_the_offer_with_its_reason(
    client, applicant, draft, reference, team
):
    """The refusal must say the sign-off is missing, not merely that the action is unavailable."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], complete=False)
    res = client.post(f"{API}/applications/{draft['id']}/offer", headers=team["admissions"].headers,
                      json={"offer_type": "conditional", "conditions": ["Pay the deposit"]})  # fmt: skip
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ERR_SIGNOFF_INCOMPLETE"


def test_an_approved_application_can_still_be_returned_before_the_offer(
    client, applicant, draft, reference, team
):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    res = client.post(f"{API}/applications/{draft['id']}/request-info", headers=team["admissions"].headers,
                      json={"reason": "A reference has not stood up; please supply another."})  # fmt: skip
    assert res.status_code == 200, res.text
    detail = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()
    assert detail["status"] == "info_requested"


# --- Cases the independent review found missing ----------------------------------------------------
def test_taking_a_verification_back_is_reflected_in_the_evidence_pack(
    client, applicant, draft, reference, team
):
    """The pack is the FR-24 deliverable, so the negative direction has to reach it too."""
    import zipfile

    doc = upload(client, applicant, draft["id"]).json()
    client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers, json={"verified": True})
    client.post(f"{API}/documents/{doc['id']}/verify", headers=team["admissions"].headers, json={"verified": False})
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)

    job = client.post(f"{API}/applications/{draft['id']}/evidence-pack", headers=team["senior"].headers).json()
    download = client.get(f"{API}/jobs/{job['id']}/download", headers=team["senior"].headers)
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    index = pdf_text(archive.read(next(n for n in archive.namelist() if n.endswith("index.pdf"))))
    assert "Not verified" in index


def test_a_quarantined_document_cannot_be_verified(client, applicant, draft, team):
    """The scan is what makes a file safe to look at; verifying one that failed it is meaningless."""
    client.post(
        f"{API}/applications/{draft['id']}/documents", headers=applicant.headers,
        data={"category": "passport"},
        files={"file": ("bad.pdf", b"%PDF-1.4\n" + EICAR, "application/pdf")},
    )  # fmt: skip
    documents = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()["documents"]
    doc_id = next(d["id"] for d in documents if d["scan_status"] == "quarantined")
    refused = client.post(f"{API}/documents/{doc_id}/verify", headers=team["admissions"].headers,
                          json={"verified": True})  # fmt: skip
    assert refused.status_code == 409


def test_an_approved_application_can_still_be_rejected_before_the_offer(
    client, applicant, draft, reference, team
):
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    res = client.post(f"{API}/applications/{draft['id']}/reject", headers=team["admissions"].headers,
                      json={"reason": "A duplicate application came to light after sign-off."})  # fmt: skip
    assert res.status_code == 200, res.text
    detail = client.get(f"{API}/applications/{draft['id']}", headers=team["admissions"].headers).json()
    assert detail["status"] == "rejected"


def test_an_approved_application_can_still_be_withdrawn(client, applicant, draft, reference, team):
    """_WITHDRAWABLE gained the new status; this is the assertion behind that line."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"])
    res = client.post(f"{API}/applications/{draft['id']}/withdraw", headers=applicant.headers,
                      json={"reason": "Accepted a place elsewhere."})  # fmt: skip
    assert res.status_code == 200, res.text
    assert client.get(f"{API}/applications/{draft['id']}",
                      headers=team["admissions"].headers).json()["status"] == "withdrawn"  # fmt: skip


def test_a_breach_after_a_recovery_alerts_again_straight_away(
    client, applicant, draft, reference, team, session
):
    """The hold-off belongs to the breach, not the calendar: a student who recovers and then
    breaches again has a new breach, and silencing it would be a missed FR-78 event."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)
    student = client.get(f"{API}/applications/{draft['id']}/student", headers=team["compliance"].headers).json()
    enrol(client, team["compliance"], student["id"])
    for i, status in enumerate(["absent", "absent", "present"]):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"M{i}", status=status)  # fmt: skip

    assert engagement_check(session) == 1
    session.commit()

    # Attendance recovers: enough present sessions to clear the threshold.
    for i in range(3, 12):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"M{i}", status="present")  # fmt: skip
    assert engagement_check(session, today=date.today() + timedelta(days=1)) == 0
    session.commit()

    # And falls again, well inside the hold-off window. This must speak.
    for i in range(12, 30):
        attend(client, team["compliance"], student["id"], day=date.today() - timedelta(days=i),
               module=f"N{i}", status="absent")  # fmt: skip
    assert engagement_check(session, today=date.today() + timedelta(days=2)) == 1


def test_a_wrong_english_standard_can_be_corrected_before_the_assessment_is_completed(
    client, applicant, draft, reference, team
):
    """It could be set but not cleared: the service keyed on 'not None', and the screen sends null."""
    submit_application(client, applicant, draft["id"], reference)
    do_academic(client, team["admissions"], draft["id"], complete=False, english_standard="CEFR B1 (typo)")
    res = do_academic(client, team["admissions"], draft["id"], complete=False,
                      english_evidence="degree_taught_in_english", english_standard=None)  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["english_standard"] is None


def test_a_test_based_route_must_name_the_standard_it_was_checked_against(
    client, applicant, draft, reference, team
):
    """FR-36 asks for the standard, and for a test that threshold is the entire check."""
    submit_application(client, applicant, draft["id"], reference)
    res = do_academic(client, team["admissions"], draft["id"], english_evidence="slt_test", english_standard=None)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "ERR_ENGLISH_STANDARD_REQUIRED"


def test_the_register_export_neutralises_a_formula_in_an_applicant_name(
    client, applicant, draft, reference, team, owner_engine
):
    """The export is built for Excel and handed to third parties. A name beginning with '=' is a
    live formula when that file is opened, so it must not leave here as one."""
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assign_cas(client, team["senior"], draft["id"], reference)
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE applications SET data = jsonb_set(data, '{full_name}', :name) WHERE id = :id"),
            {"name": '"=HYPERLINK(\\"https://example.invalid\\",\\"click\\")"', "id": draft["id"]},
        )

    res = client.get(f"{API}/cas/export", headers=team["compliance"].headers)
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[1][2].startswith("'=HYPERLINK")
