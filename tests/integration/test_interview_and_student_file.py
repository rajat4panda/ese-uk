"""The credibility interview (FR-37; prototype screen 6) and the student's Notes and Documents
(prototype screen 12).

An interview ticked as held must say who held it, when and what it found, and notes ticked as
uploaded must actually be on file — a tick with nothing behind it is not evidence. Case notes are
append-only: the database itself refuses to let the application change or delete one.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from tests.fixtures import API, PDF_BYTES, assign_cas, run_to_precas, student_for, submit_application


@pytest.fixture
def team(staff):
    return {"admissions": staff("admissions"), "compliance": staff("compliance"),
            "finance": staff("finance"), "senior": staff("senior")}  # fmt: skip


@pytest.fixture
def submitted(client, applicant, draft, reference):
    submit_application(client, applicant, draft["id"], reference)
    return draft["id"]


ALL_CHECKS = {"qualifications_verified": True, "entry_requirements_met": True, "progression_appropriate": True,
              "english_evidence_verified": True}  # fmt: skip
ENGLISH = {"english_evidence": "majority_english_country"}


def academic(client, actor, app_id, **body):
    return client.post(f"{API}/applications/{app_id}/academic", json=body, headers=actor.headers)


def upload_notes(client, actor, app_id):
    return client.post(f"{API}/applications/{app_id}/documents", data={"category": "interview_notes"},
                       files={"file": ("interview.pdf", PDF_BYTES, "application/pdf")}, headers=actor.headers)  # fmt: skip


def test_interview_is_recorded_and_returned(client, submitted, team, owner_engine):
    res = academic(client, team["admissions"], submitted, checklist={"interview_completed": True},
                   interview_date=(date.today() - timedelta(days=1)).isoformat(), interviewer="Dr A. Rossi",
                   interview_outcome="satisfactory", interview_notes="Clear study plan; funding explained.")  # fmt: skip
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["interviewer"], body["interview_outcome"]) == ("Dr A. Rossi", "satisfactory")
    with owner_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_entries WHERE action = 'academic.save' "
                                   "ORDER BY id DESC LIMIT 1")).scalar()  # fmt: skip
    assert "interview" in detail and "Dr A. Rossi: satisfactory" in detail


def test_an_interview_ticked_as_held_must_be_recorded(client, submitted, team):
    res = academic(client, team["admissions"], submitted, checklist={**ALL_CHECKS, "interview_completed": True},
                   outcome="recommend_offer", complete=True, **ENGLISH)  # fmt: skip
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "ERR_INTERVIEW_INCOMPLETE"
    assert {d["field"] for d in res.json()["error"]["details"]} == {"interview_date", "interviewer", "interview_outcome"}


def test_notes_ticked_as_uploaded_must_be_on_file(client, submitted, team):
    res = academic(client, team["admissions"], submitted, checklist={**ALL_CHECKS, "interview_notes_uploaded": True},
                   outcome="recommend_offer", complete=True, **ENGLISH)  # fmt: skip
    assert res.status_code == 422 and res.json()["error"]["code"] == "ERR_INTERVIEW_NOTES_MISSING"
    assert upload_notes(client, team["admissions"], submitted).status_code == 201
    res = academic(client, team["admissions"], submitted, checklist={**ALL_CHECKS, "interview_notes_uploaded": True},
                   outcome="recommend_offer", complete=True, **ENGLISH)  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["interview_notes_documents"] == 1


def test_applicant_cannot_upload_interview_notes(client, applicant, draft):
    assert upload_notes(client, applicant, draft["id"]).status_code == 403


def test_the_applicant_cannot_see_or_download_interview_notes(client, submitted, team, applicant):
    doc_id = upload_notes(client, team["admissions"], submitted).json()["id"]
    mine = client.get(f"{API}/applications/{submitted}", headers=applicant.headers).json()
    assert doc_id not in [d["id"] for d in mine["documents"]]
    assert client.get(f"{API}/documents/{doc_id}/download", headers=applicant.headers).status_code == 404


def test_interview_date_cannot_be_in_the_future(client, submitted, team):
    res = academic(client, team["admissions"], submitted, interview_date=(date.today() + timedelta(days=3)).isoformat())
    assert res.status_code == 422


def test_an_interview_field_can_be_cleared(client, submitted, team):
    academic(client, team["admissions"], submitted, interviewer="Wrong Person")
    assert academic(client, team["admissions"], submitted, interviewer=None).json()["interviewer"] is None


# --- Student record: notes and documents --------------------------------------------------------------
@pytest.fixture
def student(client, applicant, draft, reference, team):
    run_to_precas(client, applicant, draft["id"], reference, team["admissions"], team["compliance"],
                  team["finance"])  # fmt: skip
    assert assign_cas(client, team["senior"], draft["id"], reference).status_code == 201
    return student_for(client, team["compliance"], draft["id"])


def test_notes_are_added_listed_newest_first_and_audited(client, student, team, owner_engine):
    for body in ("Called the student about attendance.", "Student confirmed new UK address by email."):
        assert client.post(f"{API}/students/{student['id']}/notes", json={"body": body},
                           headers=team["compliance"].headers).status_code == 201  # fmt: skip
    rows = client.get(f"{API}/students/{student['id']}/notes", headers=team["admissions"].headers).json()
    assert [r["body"] for r in rows] == ["Student confirmed new UK address by email.", "Called the student about attendance."]
    assert rows[0]["author"]
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_entries WHERE action = 'student.note'")).scalar() == 2


def test_notes_cannot_be_changed_or_deleted_by_the_application(client, student, team):
    client.post(f"{API}/students/{student['id']}/notes", json={"body": "Original note."}, headers=team["compliance"].headers)
    engine = create_engine(get_settings().DATABASE_URL)  # the application's own login
    with engine.connect() as conn:
        for sql in ("UPDATE student_notes SET body = 'rewritten'", "DELETE FROM student_notes"):
            with pytest.raises(DBAPIError):
                conn.execute(text(sql))
            conn.rollback()
    engine.dispose()


def test_finance_cannot_read_notes(client, student, team):
    assert client.get(f"{API}/students/{student['id']}/notes", headers=team["finance"].headers).status_code == 403


def test_student_documents_include_new_uploads(client, student, team):
    res = client.post(f"{API}/students/{student['id']}/documents", data={"category": "passport"},
                      files={"file": ("renewed-passport.pdf", PDF_BYTES, "application/pdf")},
                      headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 201, res.text
    docs = client.get(f"{API}/students/{student['id']}/documents", headers=team["compliance"].headers).json()
    assert "renewed-passport.pdf" in [d["filename"] for d in docs]
    assert client.get(f"{API}/documents/{res.json()['id']}/download", headers=team["admissions"].headers).status_code == 200


def test_student_document_category_is_restricted(client, student, team):
    res = client.post(f"{API}/students/{student['id']}/documents", data={"category": "agent_agreement"},
                      files={"file": ("x.pdf", PDF_BYTES, "application/pdf")}, headers=team["compliance"].headers)  # fmt: skip
    assert res.status_code == 400
