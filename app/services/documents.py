"""Document upload pipeline and retrieval (LLD 11).

Upload order, with one deliberate strengthening of LLD 11.1: the malware scan runs on the
in-memory bytes *before* the object is written, so infected content never reaches storage at all.
A quarantined upload still leaves a document row and two audit entries as evidence (FR-22).
If the scanner is unreachable the upload is refused outright (fail closed) and nothing is kept.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import PurePath

from sqlalchemy.orm import Session

from app.errors import BadRequest, Conflict, Forbidden, GuardFailed, NotFound, ServiceUnavailable
from app.models.application import Application, Document
from app.models.enums import (
    APPLICANT_DOCUMENT_CATEGORIES,
    EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    DocumentCategory,
    ScanStatus,
    UserRole,
)
from app.models.user import User
from app.services import audit
from app.services.scanning import ScannerUnavailable, get_scanner
from app.services.storage import get_storage

log = logging.getLogger("ese.documents")

# Magic bytes (LLD 11.1 step 2). Extension and client content type are advisory only.
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)

DOWNLOAD_ROLES = frozenset({UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})
UPLOAD_STAFF_ROLES = DOWNLOAD_ROLES  # LLD 6: document upload — not finance
#: Held against an agent (BRD FR-91), never against an application or a student.
AGENT_DOCUMENT_CATEGORIES = frozenset({DocumentCategory.AGENT_AGREEMENT, DocumentCategory.AGENT_DUE_DILIGENCE})


def sniff(head: bytes) -> tuple[str, str] | None:
    for magic, content_type, ext in SIGNATURES:
        if head.startswith(magic):
            return content_type, ext
    return None


def safe_filename(name: str | None) -> str:
    base = PurePath((name or "upload").replace("\\", "/")).name.strip() or "upload"
    base = "".join(ch for ch in base if ch.isprintable())
    return base[:255]


def upload(
    db: Session,
    app: Application,
    actor: User,
    *,
    category: DocumentCategory,
    filename: str | None,
    data: bytes,
    max_bytes: int,
    expiry_date: date | None = None,
    ip: str | None = None,
) -> Document:
    # Authorisation (LLD 6) — ownership is checked by the caller via get_for_actor.
    if actor.role == UserRole.APPLICANT:
        if app.status not in EDITABLE_STATUSES:
            raise Conflict("ERR_NOT_EDITABLE", "Documents can only be added while your application is editable.")
        if category not in APPLICANT_DOCUMENT_CATEGORIES:
            raise Forbidden(message="This document category is added by ESE staff.")
    elif actor.role not in UPLOAD_STAFF_ROLES:
        raise Forbidden()
    if app.status in TERMINAL_STATUSES:
        raise Conflict("ERR_INVALID_STATE", "Documents cannot be added to a closed application.")
    if category in AGENT_DOCUMENT_CATEGORIES:
        raise BadRequest("ERR_INVALID_FORMAT",
                         "Agent agreements and due-diligence records are held on the agent register.",
                         details=[{"field": "category", "code": "ERR_INVALID_FORMAT"}])  # fmt: skip

    # Steps 1-2: size and magic bytes
    if len(data) > max_bytes:
        raise BadRequest("ERR_FILE_SIZE", f"Files must be {max_bytes // (1024 * 1024)}MB or smaller.",
                         details=[{"field": "file", "code": "ERR_FILE_SIZE"}])  # fmt: skip
    if not data:
        raise BadRequest("ERR_FILE_TYPE", "The file is empty.", details=[{"field": "file", "code": "ERR_FILE_TYPE"}])
    detected = sniff(data[:512])
    if detected is None:
        raise BadRequest(
            "ERR_FILE_TYPE",
            "Upload a PDF, JPG or PNG file.",
            details=[{"field": "file", "code": "ERR_FILE_TYPE"}],
        )
    content_type, ext = detected

    # Step 3: opaque storage key
    storage_key = f"{app.ref}/{category.value}/{uuid.uuid4().hex}{ext}"
    display_name = safe_filename(filename)

    # Step 5 (moved ahead of storage): scan
    try:
        result = get_scanner().scan(data)
    except ScannerUnavailable as exc:
        log.error("Malware scanner unavailable: %s", exc)
        raise ServiceUnavailable(
            "ERR_SCAN_UNAVAILABLE", "Documents cannot be accepted right now. Please try again shortly."
        ) from exc

    doc = Document(
        application_id=app.id,
        category=category,
        filename=display_name,
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=len(data),
        scan_status=ScanStatus.CLEAN.value if result.clean else ScanStatus.QUARANTINED.value,
        expiry_date=expiry_date,
        uploaded_by=actor.id,
    )
    db.add(doc)
    db.flush()
    audit.write(
        db, actor, "document.upload", application=app, entity_type="document", entity_id=doc.id,
        detail=f"{category.value}: {display_name} ({content_type}, {len(data)} bytes)", ip=ip,
    )  # fmt: skip

    if not result.clean:
        audit.write(
            db, None, "document.quarantine", application=app, entity_type="document", entity_id=doc.id,
            detail=f"Malware scan failed ({result.signature}); file not stored", ip=ip,
        )  # fmt: skip
        log.error("SECURITY: quarantined upload doc=%s app=%s signature=%s", doc.id, app.ref, result.signature)
        # Admin notification (FR-22) is routed through the notification queue from Sprint 3.
        raise GuardFailed(
            "ERR_SCAN_FAILED",
            "This file failed the security scan and has not been accepted.",
            details=[{"field": "file", "code": "ERR_SCAN_FAILED"}],
        )

    # Step 4: store with SSE (S3 backend)
    get_storage().put(storage_key, data, content_type)
    return doc


def ingest(
    db: Session,
    actor: User,
    *,
    data: bytes,
    filename: str | None,
    max_bytes: int,
    category: DocumentCategory,
    key_prefix: str,
    application_id: int | None = None,
    student_id: int | None = None,
    describe: str = "",
    ip: str | None = None,
) -> Document:
    """The upload pipeline for staff-held evidence, kept identical to upload() above: size, type by
    magic bytes, malware scan before storage, a quarantine row and audit entries if the scan fails.
    The caller has already decided who may upload and to what; it must commit on GuardFailed too, so
    a quarantined file leaves its evidence behind (FR-22)."""
    if len(data) > max_bytes:
        raise BadRequest("ERR_FILE_SIZE", f"Files must be {max_bytes // (1024 * 1024)}MB or smaller.",
                         details=[{"field": "file", "code": "ERR_FILE_SIZE"}])  # fmt: skip
    if not data:
        raise BadRequest("ERR_FILE_TYPE", "The file is empty.", details=[{"field": "file", "code": "ERR_FILE_TYPE"}])
    detected = sniff(data[:512])
    if detected is None:
        raise BadRequest("ERR_FILE_TYPE", "Upload a PDF, JPG or PNG file.",
                         details=[{"field": "file", "code": "ERR_FILE_TYPE"}])  # fmt: skip
    content_type, ext = detected
    try:
        result = get_scanner().scan(data)
    except ScannerUnavailable as exc:
        log.error("Malware scanner unavailable: %s", exc)
        raise ServiceUnavailable(
            "ERR_SCAN_UNAVAILABLE", "Documents cannot be accepted right now. Please try again shortly."
        ) from exc
    display_name = safe_filename(filename)
    doc = Document(
        application_id=application_id, student_id=student_id, category=category, filename=display_name,
        storage_key=f"{key_prefix}/{category.value}/{uuid.uuid4().hex}{ext}", content_type=content_type,
        size_bytes=len(data), scan_status=ScanStatus.CLEAN.value if result.clean else ScanStatus.QUARANTINED.value,
        uploaded_by=actor.id,
    )  # fmt: skip
    db.add(doc)
    db.flush()
    audit.write(db, actor, "document.upload", application_id=application_id, student_id=student_id,
                entity_type="document", entity_id=doc.id,
                detail=f"{describe + ': ' if describe else ''}{category.value}: {display_name} "
                       f"({content_type}, {len(data)} bytes)", ip=ip)  # fmt: skip
    if not result.clean:
        audit.write(db, None, "document.quarantine", application_id=application_id, student_id=student_id,
                    entity_type="document", entity_id=doc.id,
                    detail=f"Malware scan failed ({result.signature}); file not stored", ip=ip)  # fmt: skip
        log.error("SECURITY: quarantined upload doc=%s signature=%s", doc.id, result.signature)
        raise GuardFailed("ERR_SCAN_FAILED", "This file failed the security scan and has not been accepted.",
                          details=[{"field": "file", "code": "ERR_SCAN_FAILED"}])  # fmt: skip
    get_storage().put(doc.storage_key, data, content_type)
    return doc


def open_for_download(db: Session, doc_id: int, actor: User, ip: str | None) -> tuple[Document, Iterator[bytes]]:
    doc = db.get(Document, doc_id)
    if doc is None or doc.application_id is None:
        raise NotFound()
    app = db.get(Application, doc.application_id)
    # An applicant may download only a file they uploaded: staff-held evidence on their application
    # (interview notes, reporting evidence, enrolment evidence) is not theirs to take (LLD 6).
    is_owner = (actor.role == UserRole.APPLICANT and app is not None and app.applicant_id == actor.id
                and doc.uploaded_by == actor.id)  # fmt: skip
    if not is_owner and actor.role not in DOWNLOAD_ROLES:
        raise NotFound() if actor.role == UserRole.APPLICANT else Forbidden()
    if doc.scan_status != ScanStatus.CLEAN:
        raise Conflict("ERR_INVALID_STATE", "This document is not available for download.")
    audit.write(
        db, actor, "document.view", application=app, entity_type="document", entity_id=doc.id,
        detail=f"Downloaded {doc.category}: {doc.filename}", ip=ip,
    )  # fmt: skip
    return doc, get_storage().open(doc.storage_key)


#: Who may say a document has been checked against its original (FR-24, LLD 6). Finance is out for
#: the same reason it is out of upload and download: it has the money side and no sight of evidence.
VERIFY_ROLES = DOWNLOAD_ROLES


def set_verified(
    db: Session, doc_id: int, actor: User, *, verified: bool, ip: str | None = None
) -> Document:
    """FR-24: record that a member of staff has checked this document, or take that back.

    Verification is a statement about evidence, so it carries the person and the moment, and both
    directions are audited — a mistaken tick that could be quietly removed would be worth nothing
    to a visiting officer. A document is only verifiable once the scan has cleared it and while it
    is still the current version of its category.
    """
    doc = db.get(Document, doc_id)
    if doc is None or doc.application_id is None:
        raise NotFound()
    if actor.role not in VERIFY_ROLES:
        raise Forbidden()
    app = db.get(Application, doc.application_id)
    if doc.scan_status != ScanStatus.CLEAN:
        raise Conflict("ERR_INVALID_STATE", "Only a document that has passed the security scan can be verified.")
    if doc.superseded_by is not None:
        raise Conflict("ERR_INVALID_STATE", "This document has been replaced by a newer upload.")

    if verified:
        if doc.verified_at is not None:
            raise Conflict("ERR_INVALID_STATE", "This document is already marked as verified.")
        doc.verified_by = actor.id
        doc.verified_at = now()
        detail = f"Verified {doc.category}: {doc.filename}"
    else:
        if doc.verified_at is None:
            raise Conflict("ERR_INVALID_STATE", "This document is not marked as verified.")
        doc.verified_by = None
        doc.verified_at = None
        detail = f"Verification removed from {doc.category}: {doc.filename}"

    db.flush()
    audit.write(
        db, actor, "document.verify", application=app, entity_type="document", entity_id=doc.id,
        detail=detail, ip=ip,
    )  # fmt: skip
    return doc


def now() -> datetime:
    return datetime.now(UTC)
