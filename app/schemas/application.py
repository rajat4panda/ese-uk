"""Application form data dictionary (LLD Appendix A) and application responses.

Two levels of validation, deliberately separated:
  * Save (autosave, FR-04): every field is optional, but any value supplied must be well formed.
    A half-completed form therefore saves, and a malformed value never reaches the database.
  * Submit (FR-05): completeness, conditional mandatories and cross-field rules, evaluated by
    services.applications.submission_problems() so that every omission is reported at once.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.schemas.common import (
    ALNUM_3_20,
    E164,
    StrictModel,
    blank_to_none,
    check_country,
    err,
    length,
    normalise_email,
    not_future,
)

Opt = Annotated[Any, BeforeValidator(blank_to_none)]
OptStr = Annotated[str | None, BeforeValidator(blank_to_none)]
OptDate = Annotated[date | None, BeforeValidator(blank_to_none)]

QualificationLevel = Literal[
    "secondary_school", "foundation", "diploma", "bachelors", "masters", "doctorate", "other"
]
HowHeard = Literal[
    "search_engine", "social_media", "ese_website", "agent", "education_fair", "friend_or_family", "other"
]
FundingSource = Literal["self", "family", "sponsor", "scholarship", "loan"]
EnglishRoute = Literal["majority_english_country", "degree_taught_in_english", "slt_test", "provider_assessment"]


# --- Nested objects ----------------------------------------------------------------------------
class Address(StrictModel):
    line1: OptStr = None
    line2: OptStr = None
    city: OptStr = None
    region: OptStr = None
    postcode: OptStr = None
    country: OptStr = None

    @field_validator("line1", "line2", "city", "region")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "This field")

    @field_validator("postcode")
    @classmethod
    def _postcode(cls, v: str | None) -> str | None:
        return length(v, 1, 20, "Postcode")

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return check_country(v)


class EmergencyContact(StrictModel):
    name: OptStr = None
    relationship: OptStr = None
    phone: OptStr = None
    email: OptStr = None

    @field_validator("name", "relationship")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "This field")

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v.replace(" ", "")):
            raise err("ERR_INVALID_FORMAT", "Enter the number in international format, for example +447700900123.")
        return v.replace(" ", "") if v else v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalise_email(v)


class Qualification(StrictModel):
    institution: OptStr = None
    title: OptStr = None
    level: QualificationLevel | None = None
    country: OptStr = None
    start_date: OptDate = None
    end_date: OptDate = None
    grade: OptStr = None

    @field_validator("institution", "title")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "This field")

    @field_validator("grade")
    @classmethod
    def _grade(cls, v: str | None) -> str | None:
        return length(v, 1, 100, "Grade")

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return check_country(v)

    @field_validator("start_date")
    @classmethod
    def _start(cls, v: date | None) -> date | None:
        return not_future(v, "Start date")


class Employment(StrictModel):
    employer: OptStr = None
    role: OptStr = None
    start_date: OptDate = None
    end_date: OptDate = None

    @field_validator("employer", "role")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "This field")


class TestScores(StrictModel):
    overall: float | None = Field(default=None, ge=0, le=120)
    reading: float | None = Field(default=None, ge=0, le=120)
    writing: float | None = Field(default=None, ge=0, le=120)
    listening: float | None = Field(default=None, ge=0, le=120)
    speaking: float | None = Field(default=None, ge=0, le=120)


class PreviousUKStudy(StrictModel):
    institution: OptStr = None
    sponsor_licence_number: OptStr = None
    qualification_level: QualificationLevel | None = None
    start_date: OptDate = None
    end_date: OptDate = None
    completed: bool | None = None

    @field_validator("institution")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "Institution")

    @field_validator("sponsor_licence_number")
    @classmethod
    def _licence(cls, v: str | None) -> str | None:
        return length(v, 1, 30, "Sponsor licence number")


class UKPermission(StrictModel):
    type: OptStr = None
    expiry_date: OptDate = None

    @field_validator("type")
    @classmethod
    def _len(cls, v: str | None) -> str | None:
        return length(v, 1, 100, "Permission type")


# --- The wizard payload ------------------------------------------------------------------------
class ApplicationData(StrictModel):
    """Every client-writable key of applications.data. Server-set keys (submitted_at,
    declaration_at, privacy_notice_version) are not listed, so a client cannot write them."""

    # Step 1 — Personal details
    full_name: OptStr = None
    other_names: OptStr = None
    date_of_birth: OptDate = None
    sex: Literal["male", "female", "prefer_not_to_say"] | None = None
    country_of_birth: OptStr = None
    email: OptStr = None
    mobile: OptStr = None
    # Step 2 — Identity and nationality
    nationality: OptStr = None
    second_nationality: OptStr = None
    passport_number: OptStr = None
    passport_issue_date: OptDate = None
    passport_expiry_date: OptDate = None
    passport_issuing_country: OptStr = None
    # Step 3 — Address and contact
    home_address: Address | None = None
    uk_address: Address | None = None
    emergency_contact: EmergencyContact | None = None
    # Step 4 — Programme and intake
    programme_id: int | None = Field(default=None, gt=0)
    intake_id: int | None = Field(default=None, gt=0)
    study_mode: Literal["full_time", "part_time"] | None = None
    agent_id: int | None = Field(default=None, gt=0)
    how_heard: HowHeard | None = None
    # Step 5 — Academic background
    qualifications: list[Qualification] | None = Field(default=None, max_length=10)
    highest_level: QualificationLevel | None = None
    employment_history: list[Employment] | None = Field(default=None, max_length=10)
    # Step 6 — English language
    english_route: EnglishRoute | None = None
    test_provider: OptStr = None
    test_reference: OptStr = None
    test_date: OptDate = None
    test_scores: TestScores | None = None
    # Step 7 — UK immigration history
    been_to_uk: bool | None = None
    previous_uk_study: list[PreviousUKStudy] | None = Field(default=None, max_length=10)
    previous_visa_refusal: bool | None = None
    refusal_details: OptStr = None
    current_uk_permission: UKPermission | None = None
    brp_number: OptStr = None
    # Step 8 — Supporting information
    personal_statement: OptStr = None
    study_purpose: OptStr = None
    funding_source: FundingSource | None = None
    funder_details: OptStr = None
    disability_support_required: bool | None = None
    # Step 9 — Declaration
    declaration_truthful: bool | None = None
    declaration_data_consent: bool | None = None
    declaration_signature_name: OptStr = None

    @field_validator("full_name", "declaration_signature_name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return length(" ".join(v.split()) if v else v, 1, 200, "Full name")

    @field_validator("other_names")
    @classmethod
    def _other(cls, v: str | None) -> str | None:
        return length(v, 1, 200, "Other names")

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date | None) -> date | None:
        if v is not None and (v > date.today() or v.year < 1900):
            raise err("ERR_DOB_RANGE", "Enter a valid date of birth.")
        return v

    @field_validator(
        "country_of_birth", "nationality", "second_nationality", "passport_issuing_country"
    )
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return check_country(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return normalise_email(v)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.replace(" ", "")
        if not E164.match(v):
            raise err("ERR_INVALID_FORMAT", "Enter the number in international format, for example +447700900123.")
        return v

    @field_validator("passport_number")
    @classmethod
    def _passport(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = "".join(v.split()).upper()
        if not ALNUM_3_20.match(v):
            raise err("ERR_INVALID_FORMAT", "Passport number must be 3 to 20 letters and numbers.")
        return v

    @field_validator("brp_number")
    @classmethod
    def _brp(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = "".join(v.split()).upper()
        if not ALNUM_3_20.match(v):
            raise err("ERR_INVALID_FORMAT", "Enter the number as shown on the card.")
        return v

    @field_validator("passport_issue_date")
    @classmethod
    def _issue(cls, v: date | None) -> date | None:
        return not_future(v, "Issue date")

    @field_validator("test_date")
    @classmethod
    def _test_date(cls, v: date | None) -> date | None:
        return not_future(v, "Test date")

    @field_validator("test_provider")
    @classmethod
    def _provider(cls, v: str | None) -> str | None:
        return length(v, 1, 100, "Test provider")

    @field_validator("test_reference")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return length(v, 1, 50, "Test reference")

    @field_validator("refusal_details", "personal_statement", "study_purpose")
    @classmethod
    def _long_text(cls, v: str | None) -> str | None:
        return length(v, 1, 4000, "This answer")

    @field_validator("funder_details")
    @classmethod
    def _funder(cls, v: str | None) -> str | None:
        return length(v, 1, 2000, "Funder details")


SERVER_KEYS = ("submitted_at", "declaration_at", "privacy_notice_version", "info_request_reason",
               "rejection_reason")  # fmt: skip

# Wizard steps (Appendix A). Field-to-step mapping lets the client navigate to a failed field.
STEPS: list[tuple[int, str, tuple[str, ...]]] = [
    (1, "Personal details", ("full_name", "other_names", "date_of_birth", "sex", "country_of_birth", "email",
                             "mobile")),
    (2, "Identity and nationality", ("nationality", "second_nationality", "passport_number", "passport_issue_date",
                                     "passport_expiry_date", "passport_issuing_country")),
    (3, "Address and contact", ("home_address", "uk_address", "emergency_contact")),
    (4, "Programme and intake", ("programme_id", "intake_id", "study_mode", "agent_id", "how_heard")),
    (5, "Academic background", ("qualifications", "highest_level", "employment_history")),
    (6, "English language", ("english_route", "test_provider", "test_reference", "test_date", "test_scores")),
    (7, "UK immigration history", ("been_to_uk", "previous_uk_study", "previous_visa_refusal", "refusal_details",
                                   "current_uk_permission", "brp_number")),
    (8, "Supporting information", ("personal_statement", "study_purpose", "funding_source", "funder_details",
                                   "disability_support_required")),
    (9, "Declaration", ("declaration_truthful", "declaration_data_consent", "declaration_signature_name")),
]  # fmt: skip
FIELD_STEP: dict[str, int] = {f: n for n, _, fields in STEPS for f in fields}


def step_for(field: str | None) -> int | None:
    if not field:
        return None
    return FIELD_STEP.get(field.split(".", 1)[0])


# --- Requests and responses --------------------------------------------------------------------
class ApplicationSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]


class DocumentVerifyIn(BaseModel):
    """FR-24. False takes the verification back, which is audited in its own right."""

    verified: bool = True


class DocumentOut(BaseModel):
    id: int
    category: str
    filename: str
    content_type: str
    size_bytes: int
    scan_status: str
    expiry_date: date | None
    verified_at: datetime | None
    verified_by_name: str | None = None
    superseded_by: int | None
    uploaded_at: datetime


class ApplicationOut(BaseModel):
    id: int
    ref: str
    status: str
    applicant_id: int
    programme_id: int | None
    intake_id: int | None
    agent_id: int | None
    data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    editable: bool
    documents: list[DocumentOut]
    outstanding_documents: list[str]


class ApplicationSummary(BaseModel):
    id: int
    ref: str
    status: str
    full_name: str | None
    email: str | None
    programme: str | None
    intake: str | None
    created_at: datetime
    updated_at: datetime


class Page(BaseModel):
    items: list[ApplicationSummary]
    total: int
    page: int
    page_size: int


class DuplicateCandidate(BaseModel):
    id: int
    ref: str
    status: str
    full_name: str | None
    matched_on: list[str]


class AuditEntryOut(BaseModel):
    id: int
    at: datetime
    user_name: str
    user_role: str
    action: str
    detail: str
    entity_type: str | None
    entity_id: int | None
