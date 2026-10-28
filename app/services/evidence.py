"""Evidence pack assembly (LLD 10.1, FR-127, T-18).

What a compliance visit asks for, in one file: the application and its data, every document with
its verification status, both assessments with the names and times of their sign-offs, the offer
and its condition history, the fee position and payments, the CAS, the visa outcome, the student
record with attendance, every reportable event with its dates and SMS reference, and the complete
audit trail in order.

It is produced asynchronously (a pack with documents takes seconds, not milliseconds) as a zip
containing a PDF index plus the original files. The result expires 24 hours after it is built, so
a pack full of personal data does not sit in object storage indefinitely (LLD 13.1).
"""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound
from app.models.application import Application, Document
from app.models.audit import AuditEntry
from app.models.compliance_events import AttendanceRecord, ReportableEvent
from app.models.enums import ScanStatus
from app.models.operations import (
    EXPORT_FAILED,
    EXPORT_READY,
    EXPORT_RESULT_HOURS,
    EXPORT_RUNNING,
    ExportJob,
)
from app.models.student import StudentRecord
from app.models.user import User
from app.services import audit, pdf, students
from app.services.finance import fee_position
from app.services.storage import get_storage

MAX_DOCUMENT_BYTES = 40 * 1024 * 1024  # a pack is evidence, not an archive of everything ever uploaded


def _dt(value: datetime | None) -> str:
    return value.strftime("%d %b %Y %H:%M") if value else "—"


def _d(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def _money(value, currency: str = "GBP") -> str:  # noqa: ANN001 — Decimal | None
    return f"{currency} {value:,.2f}" if value is not None else "—"


def _address(a: dict | None) -> str:
    a = a or {}
    return ", ".join(str(a[k]) for k in ("line1", "line2", "city", "region", "postcode", "country") if a.get(k)) or "—"


def assemble(db: Session, app: Application, requested_by: User) -> dict:
    """Collect everything the pack shows. Read-only: nothing here writes."""
    student = students.for_application(db, app.id)
    position = fee_position(db, app)
    data = app.data

    summary = [
        ("Application reference", app.ref),
        ("Status", str(app.status).replace("_", " ")),
        ("Applicant", str(data.get("full_name") or app.applicant.full_name)),
        ("Date of birth", str(data.get("date_of_birth") or "—")),
        ("Nationality", str(data.get("nationality") or "—")),
        ("Passport number", str(data.get("passport_number") or "—")),
        ("Passport expiry", str(data.get("passport_expiry_date") or "—")),
        ("Programme", app.programme.title if app.programme else "—"),
        ("Intake", app.intake.label if app.intake else "—"),
        ("Submitted", str(data.get("submitted_at") or "—")),
        ("Privacy notice version", str(data.get("privacy_notice_version") or "—")),
    ]

    # FR-24: the pack names the person who verified each document, not only the moment. A date
    # with no name behind it is not evidence a visiting officer can follow up.
    def _verified(doc) -> str:  # noqa: ANN001
        if doc.verified_at is None:
            return "Not verified"
        who = db.get(User, doc.verified_by) if doc.verified_by else None
        return f"{_dt(doc.verified_at)}<br/>{who.full_name}" if who else _dt(doc.verified_at)

    documents = [
        (
            str(doc.category).replace("_", " "),
            doc.filename,
            _dt(doc.uploaded_at),
            str(doc.scan_status),
            _verified(doc),
        )
        for doc in app.documents
    ]

    assessments: list[tuple[str, list[tuple[str, str]]]] = []
    academic = app.academic_assessment
    if academic is not None:
        completer = db.get(User, academic.completed_by) if academic.completed_by else None
        assessments.append(
            (
                "Academic assessment",
                [
                    ("Outcome", (academic.outcome or "—").replace("_", " ")),
                    ("Checks satisfied", ", ".join(k for k, v in (academic.checklist or {}).items() if v) or "—"),
                    # FR-36: which English evidence was accepted, and against what.
                    ("English evidence accepted", (academic.english_evidence or "—").replace("_", " ")),
                    ("English standard", academic.english_standard or "—"),
                    ("Comments", academic.comments or "—"),
                    ("Signed off by", completer.full_name if completer else "—"),
                    ("Signed off at", _dt(academic.completed_at)),
                ],
            )
        )
    review = app.compliance_review
    if review is not None:
        completer = db.get(User, review.completed_by) if review.completed_by else None
        second = db.get(User, review.second_line_by) if review.second_line_by else None
        assessments.append(
            (
                "Compliance review",
                [
                    ("Risk rating", str(review.risk_rating) if review.risk_rating else "—"),
                    ("ATAS required", "Yes" if review.atas_required else "No"),
                    ("Checks satisfied", ", ".join(k for k, v in (review.checklist or {}).items() if v) or "—"),
                    ("Comments", review.comments or "—"),
                    ("Signed off by", completer.full_name if completer else "—"),
                    ("Signed off at", _dt(review.completed_at)),
                    ("Second-line review", f"{second.full_name} — {review.second_line_outcome}" if second else "—"),
                ],
            )
        )
    if not assessments:
        assessments.append(("Assessments", [("Status", "No assessment has been recorded.")]))

    offer = app.offer
    offer_block: dict = {"summary": [], "conditions": []}
    if offer is not None:
        offer_block["summary"] = [
            ("Offer type", offer.offer_type),
            ("Issued", _dt(offer.issued_at) + (f" by {offer.issue_method}" if offer.issue_method else "")),
            ("Expires", _d(offer.expiry_date)),
            ("Applicant response", f"{offer.response} on {_dt(offer.response_at)}" if offer.response else "—"),
            ("Conditions overridden", offer.override_reason or "—"),
        ]
        offer_block["conditions"] = [
            (c.description, c.status, _dt(c.verified_at)) for c in offer.conditions
        ]

    fees = {
        "summary": [
            ("Agreed tuition fee", _money(position.agreed_fee, position.currency)),
            ("Minimum deposit", _money(position.minimum_deposit, position.currency)),
            ("Paid", _money(position.paid, position.currency)),
            ("Scholarship", _money(position.scholarship, position.currency)),
            ("Refunded", _money(position.refunded, position.currency)),
            ("Outstanding", _money(position.outstanding, position.currency)),
            ("Deposit gate", "Satisfied" if position.deposit_met else "Not met"),
        ],
        "payments": [
            (_d(p.payment_date), str(p.type), _money(p.amount, position.currency), p.method or "—",
             p.reference or "—")  # fmt: skip
            for p in app.payments
        ],
    }

    cas_rows: list[tuple[str, str]] = []
    if app.cas is not None:
        cas = app.cas
        visa = cas.visa_outcome
        cas_rows = [
            ("CAS number", cas.cas_number),
            ("Course", cas.course_title),
            ("Course dates", f"{_d(cas.start_date)} to {_d(cas.end_date)}"),
            ("Tuition fee on CAS", _money(cas.tuition_fee)),
            ("Paid at assignment", _money(cas.amount_paid)),
            ("Level 1 user", cas.level1_user),
            ("Authorised by", cas.authoriser.full_name if cas.authoriser else "—"),
            ("Assigned", _dt(cas.assigned_at)),
            ("Allocation period", cas.allocation.period_label if cas.allocation else "—"),
            ("CAS status", (f"{cas.withdrawal_type or cas.status} with effect from {_d(cas.withdrawal_date)}"
                            if cas.withdrawal_date else cas.status)
                           + (f" — {cas.withdrawal_reason}" if cas.withdrawal_reason else "")),
            (
                "Visa outcome",
                f"{visa.outcome} on {_d(visa.decision_date)}"
                + (f" ({visa.refusal_reason_category})" if visa and visa.refusal_reason_category else "")
                if visa
                else "No decision recorded",
            ),
        ]

    student_block: dict = {"summary": [], "attendance": [], "checks": [], "contacts": []}
    events_rows: list[tuple[str, ...]] = []
    if student is not None:
        summary_att = students.attendance_summary(db, student)
        proof = db.get(Document, student.enrolment_evidence_id) if student.enrolment_evidence_id else None
        student_block["summary"] = [
            ("Enrolment status", str(student.enrolment_status).replace("_", " ")),
            ("Reason", student.enrolment_status_reason or "—"),
            ("Enrolment date", _d(student.enrolment_date)),
            ("Permission expiry", _d(student.permission_expiry)),
            ("Completion", f"{student.completion_outcome} on {_d(student.completion_date)}"
                           if student.completion_outcome else "—"),  # fmt: skip
            ("Enrolment evidence", f"{proof.filename} (document {proof.id})" if proof else "None on file"),
            ("Last status check", _d(student.last_status_check)),
            ("Current address", _address(student.current_address)),
            ("Contact", " · ".join(x for x in (student.contact_phone, student.contact_email) if x) or "—"),
        ]
        student_block["checks"] = [
            (_d(c.check_date), students.STATUS_CHECK_METHODS[c.method], c.reference or "—",
             students.STATUS_CHECK_OUTCOMES[c.outcome],
             " ".join(x for x in (c.permission_type, f"to {_d(c.permission_expiry)}" if c.permission_expiry else None)
                      if x) or "—",
             c.notes or "—")  # fmt: skip
            for c in reversed(students.status_checks(db, student))
        ]
        student_block["contacts"] = [
            (_d(c.effective_from), _address(c.address), " · ".join(x for x in (c.phone, c.email) if x) or "—",
             c.reason or "—")  # fmt: skip
            for c in reversed(students.contact_history(db, student))
        ]
        if summary_att.has_data:
            student_block["attendance"] = [
                ("Sessions recorded", str(summary_att.scheduled)),
                ("Present", str(summary_att.present)),
                ("Authorised absence", str(summary_att.authorised_absence)),
                ("Absent", str(summary_att.absent)),
                ("Attendance", f"{summary_att.percentage}% (threshold {summary_att.threshold}%)"),
            ]
        events_rows = [
            (
                str(e.event_type).replace("_", " "),
                _d(e.event_date),
                _d(e.deadline_date),
                e.status,
                _d(e.reported_date),
                e.sms_reference or (e.not_required_reason or "—"),
            )
            for e in db.scalars(
                select(ReportableEvent)
                .where(ReportableEvent.student_id == student.id)
                .order_by(ReportableEvent.event_date)
            )
        ]

    # Entries reach the trail by application or by student: a post-CAS action carries the student
    # id, and filtering on the application alone would quietly drop it from the pack.
    scope = AuditEntry.application_id == app.id
    if student is not None:
        scope = scope | (AuditEntry.student_id == student.id)
    audit_rows = [
        (_dt(entry.at), entry.user_name, entry.user_role, entry.action, entry.detail)
        for entry in db.scalars(select(AuditEntry).where(scope).order_by(AuditEntry.at, AuditEntry.id))
    ]

    return {
        "generated_at": datetime.now(UTC),
        "generated_by": requested_by.full_name,
        "application": {"ref": app.ref, "summary": summary, "documents": documents},
        "assessments": assessments,
        "offer": offer_block,
        "fees": fees,
        "cas": cas_rows,
        "student": student_block,
        "events": events_rows,
        "audit": audit_rows,
    }


def build_archive(db: Session, app: Application, requested_by: User) -> tuple[bytes, str]:
    """The zip: the PDF index plus every clean document, under its own folder."""
    pack = assemble(db, app, requested_by)
    index_pdf = pdf.evidence_pack_index(pack)

    buffer = BytesIO()
    storage = get_storage()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{app.ref}/00-evidence-pack-index.pdf", index_pdf)
        included, skipped = 0, []
        for doc in app.documents:
            if doc.scan_status != ScanStatus.CLEAN:
                skipped.append(f"{doc.filename}: not included — scan status {doc.scan_status}")
                continue
            if doc.size_bytes and doc.size_bytes > MAX_DOCUMENT_BYTES:
                skipped.append(f"{doc.filename}: not included — larger than the pack limit")
                continue
            try:
                content = b"".join(storage.open(doc.storage_key))
            except Exception as exc:  # noqa: BLE001 — a missing file is itself evidence, so record it
                skipped.append(f"{doc.filename}: could not be read from storage ({type(exc).__name__})")
                continue
            archive.writestr(f"{app.ref}/documents/{doc.id:04d}-{doc.filename}", content)
            included += 1
        manifest = [
            f"Evidence pack for {app.ref}",
            f"Generated {pack['generated_at'].isoformat()} by {pack['generated_by']}",
            f"Documents included: {included} of {len(app.documents)}",
            f"Audit entries: {len(pack['audit'])}",
            f"Reportable events: {len(pack['events'])}",
            "",
            *(skipped or ["All documents on file are included."]),
            "",
            "Not included in this build: the correspondence log (LLD 10.1). Notifications are "
            "recorded in the system but the per-message log arrives in Sprint 4, so this pack "
            "should not be read as evidence of what was sent to the applicant.",
        ]
        archive.writestr(f"{app.ref}/manifest.txt", "\n".join(manifest))
    return buffer.getvalue(), f"evidence-pack-{app.ref}.zip"


def run_export_job(db: Session, job: ExportJob) -> ExportJob:
    """Build the pack for a queued job and store it under a key that expires (LLD 13.1)."""
    job.status = EXPORT_RUNNING
    db.flush()
    app = db.get(Application, job.application_id)
    requester = db.get(User, job.requested_by)
    if app is None or requester is None:
        raise NotFound()
    try:
        content, filename = build_archive(db, app, requester)
    except Exception as exc:  # noqa: BLE001 — the failure belongs on the job, not in a stack trace
        job.status = EXPORT_FAILED
        job.error_detail = f"{type(exc).__name__}: {exc}"[:2000]
        job.finished_at = datetime.now(UTC)
        db.flush()
        raise

    key = f"{app.ref}/exports/{job.id}.zip"
    get_storage().put(key, content, "application/zip")
    job.status = EXPORT_READY
    job.result_storage_key = key
    job.result_filename = filename
    job.expires_at = datetime.now(UTC) + timedelta(hours=EXPORT_RESULT_HOURS)
    job.finished_at = datetime.now(UTC)
    db.flush()
    audit.write(
        db, requester, "data.evidence_pack", application=app, entity_type="export_job", entity_id=None,
        detail=(f"Evidence pack generated for {app.ref}: {len(content):,} bytes, "
                f"available until {job.expires_at.isoformat()}"),
    )  # fmt: skip
    return job


def attendance_for_pack(db: Session, student: StudentRecord) -> list[AttendanceRecord]:
    return students.attendance_history(db, student, limit=500)


def expired(job: ExportJob, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return job.expires_at is not None and job.expires_at < now


def documents_for(db: Session, app: Application) -> list[Document]:
    return list(app.documents)
