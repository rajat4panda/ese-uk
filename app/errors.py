"""Error model (LLD Section 15).

Every error response has the shape:
    {"error": {"code": "ERR_...", "message": "...", "details": [...], "request_id": "..."}}
The code selects the message the client renders (LLD 14.2); details carry field-level
failures so the interface can show them against the right field.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status_code: int = 400
    code: str = "ERR_BAD_REQUEST"
    message: str = "The request could not be processed."

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        status_code: int | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code or self.code
        self.message = message or self.message
        if status_code is not None:
            self.status_code = status_code
        self.details = details or []
        self.headers = headers
        super().__init__(f"{self.code}: {self.message}")


class BadRequest(ApiError):
    status_code = 400


class Unauthenticated(ApiError):
    status_code = 401
    code = "ERR_UNAUTHENTICATED"
    message = "Please sign in to continue."


class Forbidden(ApiError):
    status_code = 403
    code = "ERR_ROLE_FORBIDDEN"
    message = "Your role does not permit this action."


class NotFound(ApiError):
    status_code = 404
    code = "ERR_NOT_FOUND"
    message = "The record was not found."


class Conflict(ApiError):
    status_code = 409
    code = "ERR_INVALID_STATE"
    message = "The action conflicts with the record's current state."


class GuardFailed(ApiError):
    """A business rule or guard rejected the action (HTTP 422)."""

    status_code = 422


class Locked(ApiError):
    status_code = 423
    code = "ERR_ACCOUNT_LOCKED"
    message = "This account is temporarily locked after repeated failed sign-in attempts."


class RateLimited(ApiError):
    status_code = 429
    code = "ERR_RATE_LIMITED"
    message = "Too many requests. Please wait a moment and try again."


class ServiceUnavailable(ApiError):
    status_code = 503
    code = "ERR_SERVICE_UNAVAILABLE"
    message = "A dependent service is unavailable. Please try again shortly."


def error_body(code: str, message: str, details: list[dict[str, Any]] | None, request_id: str | None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or [], "request_id": request_id}}
