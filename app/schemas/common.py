"""Shared validation helpers. Field rules are declared once here and apply identically to the
API, the generated OpenAPI contract and the client's error rendering (LLD 9)."""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError


class StrictModel(BaseModel):
    """Whitelists writable fields: unknown keys are rejected (LLD 16 — mass assignment).
    Blank strings are treated as "not answered" so a cleared field saves as null."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_strings_to_none(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in values.items()}
        return values


@lru_cache
def country_codes() -> frozenset[str]:
    path = Path(__file__).resolve().parent.parent / "data" / "countries.json"
    return frozenset(c["code"] for c in json.loads(path.read_text(encoding="utf-8")))


def err(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(code, message)


def blank_to_none(v: Any) -> Any:
    if isinstance(v, str) and not v.strip():
        return None
    return v


def check_country(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.upper()
    if v not in country_codes():
        raise err("ERR_INVALID_FORMAT", "Select a country from the list.")
    return v


def normalise_email(v: str | None) -> str | None:
    if v is None:
        return None
    if len(v) > 255:
        raise err("ERR_INVALID_EMAIL", "Email address must be 255 characters or fewer.")
    try:
        result = validate_email(v, check_deliverability=False)
    except EmailNotValidError as exc:
        raise err("ERR_INVALID_EMAIL", "Enter a valid email address.") from exc
    return result.normalized.lower()


E164 = re.compile(r"^\+[1-9]\d{6,14}$")
ALNUM_3_20 = re.compile(r"^[A-Z0-9]{3,20}$")


def not_future(v: date | None, label: str = "Date") -> date | None:
    if v is not None and v > date.today():
        raise err("ERR_DATE_RANGE", f"{label} cannot be in the future.")
    return v


def length(v: str | None, lo: int, hi: int, label: str) -> str | None:
    if v is None:
        return None
    if len(v) > hi:
        raise err("ERR_TOO_LONG", f"{label} must be {hi} characters or fewer.")
    if len(v) < lo:
        raise err("ERR_REQUIRED", f"Enter {label.lower()}.")
    return v


BUILTIN_CODE_MAP = {
    "extra_forbidden": "ERR_UNKNOWN_FIELD",
    "missing": "ERR_REQUIRED",
    "literal_error": "ERR_INVALID_OPTION",
    "enum": "ERR_INVALID_OPTION",
}


def validation_details(exc: ValidationError | Any, strip_prefix: tuple[str, ...] = ()) -> list[dict]:
    """Convert Pydantic errors to LLD 15 detail rows: {field, code, message}."""
    out: list[dict] = []
    for e in exc.errors():
        loc = [str(p) for p in e.get("loc", ())]
        while loc and loc[0] in strip_prefix:
            loc = loc[1:]
        code = e.get("type", "")
        if not code.startswith("ERR_"):
            code = BUILTIN_CODE_MAP.get(code, "ERR_INVALID_FORMAT")
        msg = e.get("msg", "Invalid value.")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        out.append({"field": ".".join(loc) or None, "code": code, "message": msg})
    return out
