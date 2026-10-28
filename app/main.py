"""Application factory, router registration, middleware (LLD 2.1)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import admin, agents, applications, auth, configuration, reference, students, workflow
from app.config import get_settings
from app.errors import ApiError, error_body
from app.schemas.application import step_for
from app.schemas.common import validation_details
from app.services.deadlines import CalendarNotPopulated

log = logging.getLogger("ese")

API_PREFIX = "/api/v1"
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
UPLOAD_BODY_LIMIT = 5_242_880 + 64 * 1024  # file limit plus multipart overhead
#: Every POST route that accepts a file. Both body-size guards below key on these suffixes, so a new
#: upload route must be added here or it escapes the limit.
UPLOAD_PATH_SUFFIXES = ("/documents", "/evidence")


def is_upload_path(path: str) -> bool:
    return path.endswith(UPLOAD_PATH_SUFFIXES)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
# Swagger UI (non-production only) loads its assets from jsdelivr; relaxed CSP applies to /api/docs alone.
DOCS_CSP = (
    "default-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id, security headers (LLD 16) and an early body-size refusal for uploads."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id[:64]

        length = request.headers.get("content-length")
        if request.method == "POST" and is_upload_path(request.url.path) and length and length.isdigit():
            if int(length) > UPLOAD_BODY_LIMIT:
                # Refuse before the body is read (LLD 11.1 step 1). The edge proxy enforces the same limit.
                resp = JSONResponse(
                    error_body("ERR_FILE_SIZE", "Files must be 5MB or smaller.",
                               [{"field": "file", "code": "ERR_FILE_SIZE"}], request.state.request_id),  # fmt: skip
                    status_code=400,
                )
                return self._secure(resp, request)

        response = await call_next(request)
        return self._secure(response, request)

    @staticmethod
    def _secure(response, request: Request):
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        is_docs = request.url.path.startswith(f"{API_PREFIX}/docs") or request.url.path.startswith("/api/docs")
        response.headers["Content-Security-Policy"] = DOCS_CSP if is_docs else CSP
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class BodyTooLarge(ApiError):
    status_code = 400
    code = "ERR_FILE_SIZE"
    message = "Files must be 5MB or smaller."


class BodySizeLimitMiddleware:
    """Counts streamed bytes on upload requests and stops at the limit, so a chunked request with no
    Content-Length cannot make the multipart parser spool an unbounded body to disk."""

    def __init__(self, app, limit: int = UPLOAD_BODY_LIMIT) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not is_upload_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLarge(details=[{"field": "file", "code": "ERR_FILE_SIZE"}])
            return message

        await self.app(scope, limited_receive, send)


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO if settings.is_production else logging.INFO)
    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENVIRONMENT, send_default_pii=False)

    docs_enabled = not settings.is_production
    app = FastAPI(
        title="ESE International Student Recruitment & Compliance System",
        version="0.1.0",
        description="eMigrant® platform — Student route module for ESE Insight Limited. LLD v1.1.",
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details, _rid(request)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = validation_details(exc, strip_prefix=("body", "query", "path", "header", "data"))
        for d in details:
            d["step"] = step_for(d["field"])
        return JSONResponse(
            error_body("ERR_VALIDATION", "Some answers need correcting.", details, _rid(request)), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        cause = exc.__cause__
        if isinstance(cause, ApiError):  # e.g. BodyTooLarge raised while FastAPI parsed the form
            return JSONResponse(error_body(cause.code, cause.message, cause.details, _rid(request)),
                                status_code=cause.status_code)  # fmt: skip
        code = {404: "ERR_NOT_FOUND", 405: "ERR_METHOD_NOT_ALLOWED", 401: "ERR_UNAUTHENTICATED"}.get(
            exc.status_code, "ERR_BAD_REQUEST"
        )
        return JSONResponse(error_body(code, str(exc.detail), None, _rid(request)), status_code=exc.status_code)

    @app.exception_handler(CalendarNotPopulated)
    async def _calendar_not_populated(request: Request, exc: CalendarNotPopulated):
        # LLD 8.2: a reporting deadline cannot be guessed. Say so plainly rather than failing
        # with a 500 that looks like a bug in the caller's request.
        return JSONResponse(
            error_body("ERR_CALENDAR_NOT_POPULATED", str(exc), None, _rid(request)),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error request_id=%s", _rid(request))
        return JSONResponse(
            error_body("ERR_INTERNAL", "Something went wrong. Quote this reference if you contact us.", None,
                       _rid(request)),  # fmt: skip
            status_code=500,
        )

    for router in (auth.router, applications.router, workflow.router, students.router, admin.router,
                   agents.router, reference.router, configuration.router):
        app.include_router(router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["operations"])
    def health() -> dict:
        from sqlalchemy import text

        from app.database import get_engine
        from app.services.cache import ping

        db_ok = True
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_ok = False
        redis_ok = ping()
        status = "ok" if db_ok and redis_ok else "degraded"
        return {"status": status, "database": db_ok, "cache": redis_ok, "environment": settings.ENVIRONMENT}

    # Serve the built single-page client from the same origin when present (CSP 'self').
    if settings.SERVE_WEB_CLIENT and WEB_DIST.is_dir():
        index = WEB_DIST / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse(error_body("ERR_NOT_FOUND", "Not found.", None, None), status_code=404)
            candidate = (WEB_DIST / full_path).resolve()
            if full_path and WEB_DIST in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
