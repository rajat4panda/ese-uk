"""Appendix A field rules (save-level validation)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.application import FIELD_STEP, STEPS, ApplicationData, step_for
from app.schemas.common import validation_details


def codes(payload: dict) -> dict[str, str]:
    with pytest.raises(ValidationError) as exc:
        ApplicationData.model_validate(payload)
    return {d["field"]: d["code"] for d in validation_details(exc.value)}


def test_every_field_belongs_to_exactly_one_step():
    listed = [f for _, _, fields in STEPS for f in fields]
    assert len(listed) == len(set(listed))
    assert set(listed) == set(ApplicationData.model_fields)
    assert step_for("home_address.city") == 3 and step_for("qualifications.0.grade") == 5
    assert len(STEPS) == 9 and FIELD_STEP["declaration_truthful"] == 9


def test_normalisation():
    d = ApplicationData.model_validate({
        "full_name": "  Priya   Raman ", "passport_number": "ab 123 45", "email": "Priya@Example.COM",
        "mobile": "+44 7700 900 123", "nationality": "gb", "brp_number": "zu 1234567",
    })  # fmt: skip
    assert d.full_name == "Priya Raman"
    assert d.passport_number == "AB12345"
    assert d.email == "priya@example.com"
    assert d.mobile == "+447700900123"
    assert d.nationality == "GB"
    assert d.brp_number == "ZU1234567"


def test_blank_strings_mean_unanswered():
    d = ApplicationData.model_validate({"sex": "", "full_name": "  ", "home_address": {"line1": ""}})
    assert d.sex is None and d.full_name is None and d.home_address.line1 is None


@pytest.mark.parametrize("payload,field,code", [
    ({"passport_number": "AB"}, "passport_number", "ERR_INVALID_FORMAT"),
    ({"passport_number": "AB-123"}, "passport_number", "ERR_INVALID_FORMAT"),
    ({"email": "not-an-email"}, "email", "ERR_INVALID_EMAIL"),
    ({"mobile": "07700900123"}, "mobile", "ERR_INVALID_FORMAT"),
    ({"nationality": "XX"}, "nationality", "ERR_INVALID_FORMAT"),
    ({"date_of_birth": (date.today() + timedelta(days=1)).isoformat()}, "date_of_birth", "ERR_DOB_RANGE"),
    ({"passport_issue_date": (date.today() + timedelta(days=1)).isoformat()}, "passport_issue_date", "ERR_DATE_RANGE"),
    ({"full_name": "x" * 201}, "full_name", "ERR_TOO_LONG"),
    ({"personal_statement": "x" * 4001}, "personal_statement", "ERR_TOO_LONG"),
    ({"sex": "other"}, "sex", "ERR_INVALID_OPTION"),
    ({"funding_source": "lottery"}, "funding_source", "ERR_INVALID_OPTION"),
    ({"programme_id": 0}, "programme_id", "ERR_INVALID_FORMAT"),
    ({"test_scores": {"overall": 500}}, "test_scores.overall", "ERR_INVALID_FORMAT"),
    ({"home_address": {"country": "ZZ"}}, "home_address.country", "ERR_INVALID_FORMAT"),
    ({"home_address": {"street": "x"}}, "home_address.street", "ERR_UNKNOWN_FIELD"),
    ({"qualifications": [{"level": "phd"}]}, "qualifications.0.level", "ERR_INVALID_OPTION"),
    ({"emergency_contact": {"phone": "123"}}, "emergency_contact.phone", "ERR_INVALID_FORMAT"),
    ({"status": "submitted"}, "status", "ERR_UNKNOWN_FIELD"),
    ({"submitted_at": "2026-01-01"}, "submitted_at", "ERR_UNKNOWN_FIELD"),
])  # fmt: skip
def test_field_rules(payload, field, code):
    assert codes(payload).get(field) == code


def test_list_limits():
    assert "qualifications" in codes({"qualifications": [{}] * 11})
