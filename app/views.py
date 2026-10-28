"""Response shaping. Responses are role-shaped: fields outside the caller's role are omitted
server-side, not hidden by the client (LLD 5.9)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.application import Application, Document
from app.models.cas import Cas, VisaOutcomeRecord
from app.models.enums import EDITABLE_STATUSES, UserRole
from app.models.offer import Offer
from app.models.student import StudentRecord
from app.models.user import User
from app.schemas.application import DocumentOut
from app.schemas.student import EventOut, StudentOut
from app.schemas.workflow import (
    AssessmentOut,
    CasOut,
    ChecklistItemOut,
    ConditionOut,
    FeePositionOut,
    OfferOut,
    PaymentOut,
    StudentRecordOut,
)
from app.services import assessments as assess_svc
from app.services import finance as finance_svc
from app.services.applications import outstanding_documents

STAFF_WITH_ASSESSMENTS = frozenset({UserRole.ADMISSIONS, UserRole.COMPLIANCE, UserRole.SENIOR, UserRole.ADMIN})


def document_out(d, db: Session | None = None) -> DocumentOut:  # noqa: ANN001
    """Who verified it matters as much as that it was verified (FR-24), so the name is resolved
    here rather than left as an id the screen cannot render."""
    name = None
    if d.verified_by is not None and db is not None:
        user = db.get(User, d.verified_by)
        name = user.full_name if user else None
    return DocumentOut(
        id=d.id, category=str(d.category), filename=d.filename, content_type=d.content_type,
        size_bytes=d.size_bytes, scan_status=d.scan_status, expiry_date=d.expiry_date,
        verified_at=d.verified_at, verified_by_name=name, superseded_by=d.superseded_by,
        uploaded_at=d.uploaded_at,
    )  # fmt: skip


def assessment_out(db: Session, app: Application, kind: str) -> AssessmentOut:
    row = app.academic_assessment if kind == "academic" else app.compliance_review
    defs = assess_svc.definitions(db, kind)  # type: ignore[arg-type]
    checklist = row.checklist if row else {}
    items = [
        ChecklistItemOut(item_key=d.item_key, label=d.label, mandatory=d.mandatory, satisfied=checklist.get(d.item_key))
        for d in defs
    ]
    completer = db.get(User, row.completed_by) if row and row.completed_by else None
    second_liner = db.get(User, row.second_line_by) if row and getattr(row, "second_line_by", None) else None
    out = AssessmentOut(
        id=row.id if row else None,
        checklist=items,
        comments=row.comments if row else "",
        completed_by=completer.full_name if completer else None,
        completed_by_id=row.completed_by if row else None,
        completed_at=row.completed_at if row else None,
        outstanding_mandatory=[d.label for d in assess_svc.outstanding_mandatory(defs, checklist)],
    )
    if kind == "academic":
        # Counted whether or not the assessment has been saved yet: the notes are often uploaded first.
        out.interview_notes_documents = assess_svc.interview_note_files(app)
    if row is not None and kind == "academic":
        out.outcome = row.outcome
        # FR-36: what the English check found, not only that it happened.
        out.english_evidence = row.english_evidence
        out.english_standard = row.english_standard
        out.interview_date = row.interview_date
        out.interviewer = row.interviewer
        out.interview_outcome = row.interview_outcome
        out.interview_notes = row.interview_notes
    elif row is not None:
        out.risk_rating = str(row.risk_rating) if row.risk_rating else None
        out.atas_required = row.atas_required
        out.atas_reference = row.atas_reference
        out.second_line_by = second_liner.full_name if second_liner else None
        out.second_line_at = row.second_line_at
        out.second_line_outcome = row.second_line_outcome
    return out


def offer_out(offer: Offer | None) -> OfferOut | None:
    if offer is None:
        return None
    return OfferOut(
        id=offer.id,
        offer_type=offer.offer_type,
        conditions=[
            ConditionOut(id=c.id, description=c.description, status=c.status,
                         evidence_document_id=c.evidence_document_id, verified_at=c.verified_at)  # fmt: skip
            for c in offer.conditions
        ],
        expiry_date=offer.expiry_date,
        issued_at=offer.issued_at,
        issue_method=offer.issue_method,
        response=offer.response,
        response_at=offer.response_at,
        override_reason=offer.override_reason,
        has_letter=offer.letter_storage_key is not None,
    )


def cas_out(cas: Cas, app: Application | None = None) -> CasOut:
    return CasOut(
        id=cas.id,
        cas_number=cas.cas_number,
        course_title=cas.course_title,
        start_date=cas.start_date,
        end_date=cas.end_date,
        tuition_fee=cas.tuition_fee,
        amount_paid=cas.amount_paid,
        level1_user=cas.level1_user,
        authorised_by=cas.authoriser.full_name if cas.authoriser else None,
        status=cas.status,
        assigned_at=cas.assigned_at,
        allocation_period=cas.allocation.period_label if cas.allocation else None,
        withdrawal_type=cas.withdrawal_type,
        withdrawal_date=cas.withdrawal_date,
        withdrawal_reason=cas.withdrawal_reason,
        application_ref=app.ref if app else None,
        applicant_name=(app.data.get("full_name") if app else None),
    )


def fee_position_out(position) -> FeePositionOut:  # noqa: ANN001
    return FeePositionOut(
        agreed_fee=position.agreed_fee, minimum_deposit=position.minimum_deposit, paid=position.paid,
        scholarship=position.scholarship, refunded=position.refunded, outstanding=position.outstanding,
        deposit_met=position.deposit_met, currency=position.currency,
    )  # fmt: skip


def payment_out(p) -> PaymentOut:  # noqa: ANN001
    return PaymentOut(id=p.id, type=str(p.type), amount=p.amount, payment_date=p.payment_date, method=p.method,
                      reference=p.reference, reason=p.reason, recorded_at=p.recorded_at)  # fmt: skip


def student_record_out(record) -> StudentRecordOut | None:  # noqa: ANN001
    if record is None:
        return None
    return StudentRecordOut(id=record.id, enrolment_status=str(record.enrolment_status),
                            enrolment_status_reason=record.enrolment_status_reason,
                            enrolment_date=record.enrolment_date)  # fmt: skip


def application_detail(db: Session, app: Application, actor: User) -> dict:
    """Everything the caller's role may see about one application (LLD 5.2, 5.9)."""
    detail: dict = {
        "id": app.id,
        "ref": app.ref,
        "status": str(app.status),
        "applicant_id": app.applicant_id,
        "applicant_name": app.data.get("full_name") or app.applicant.full_name,
        "programme_id": app.programme_id,
        "programme": app.programme.title if app.programme else None,
        "intake_id": app.intake_id,
        "intake": app.intake.label if app.intake else None,
        "agent_id": app.agent_id,
        "data": app.data,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
        "editable": app.status in EDITABLE_STATUSES,
        # Staff hold evidence against the application too — interview notes, SMS confirmations,
        # enrolment evidence. The applicant sees only the files they uploaded themselves.
        "documents": [document_out(d, db) for d in app.documents
                      if actor.role != UserRole.APPLICANT or d.uploaded_by == actor.id],
        "outstanding_documents": outstanding_documents(app),
        "offer": offer_out(app.offer),
        "cas": cas_out(app.cas, app) if app.cas else None,
        "student_record": student_record_out(app.student_record),
    }
    if actor.role in STAFF_WITH_ASSESSMENTS:
        detail["academic"] = assessment_out(db, app, "academic")
        detail["compliance"] = assessment_out(db, app, "compliance")
    if actor.role != UserRole.APPLICANT:
        detail["fee_position"] = fee_position_out(finance_svc.fee_position(db, app))
        detail["payments"] = [payment_out(p) for p in app.payments]
    return detail


# --- Sprint 3: student record, attendance and reportable events --------------------------------
def event_out(db: Session, event) -> EventOut:  # noqa: ANN001 — ReportableEvent
    from datetime import date as _date

    student = db.get(StudentRecord, event.student_id)
    app = db.get(Application, student.application_id) if student else None
    reporter = db.get(User, event.reported_by) if event.reported_by else None
    today = _date.today()
    return EventOut(
        id=event.id,
        student_id=event.student_id,
        application_id=app.id if app else None,
        application_ref=app.ref if app else None,
        student_name=(app.data.get("full_name") if app else None),
        event_type=str(event.event_type),
        event_date=event.event_date,
        deadline_date=event.deadline_date,
        days_remaining=event.days_remaining(today),
        overdue=event.is_overdue(today),
        source=event.source,
        detail=event.detail,
        status=event.status,
        reported_by=reporter.full_name if reporter else None,
        reported_date=event.reported_date,
        sms_reference=event.sms_reference,
        not_required_reason=event.not_required_reason,
        escalated_at=event.escalated_at,
        created_at=event.created_at,
    )


def student_out(db: Session, student: StudentRecord) -> StudentOut:
    from app.models.cas import Cas
    from app.schemas.student import (
        AttendanceOut,
        AttendanceSummaryOut,
        EscalationOut,
        VisaOutcomeOut,
    )
    from app.services import events as events_svc
    from app.services import students as students_svc

    app = db.get(Application, student.application_id)
    cas = db.get(Cas, student.cas_id)
    summary = students_svc.attendance_summary(db, student)
    # Queried rather than read off the relationship: within one request the outcome may have been
    # written after the CAS row was loaded, and a stale relationship would report "no decision".
    visa_row = (
        db.scalars(select(VisaOutcomeRecord).where(VisaOutcomeRecord.cas_id == cas.id)).first() if cas else None
    )
    recorder = db.get(User, visa_row.recorded_by) if visa_row else None
    checks = students_svc.status_checks(db, student)
    latest_check = checks[0] if checks else None
    evidence_doc = db.get(Document, student.enrolment_evidence_id) if student.enrolment_evidence_id else None

    return StudentOut(
        id=student.id,
        application_id=student.application_id,
        application_ref=app.ref if app else "—",
        student_name=(app.data.get("full_name") if app else None),
        programme=app.programme.title if app and app.programme else None,
        intake=app.intake.label if app and app.intake else None,
        cas_number=cas.cas_number if cas else None,
        cas_status=cas.status if cas else None,
        course_start=cas.start_date if cas else None,
        course_end=cas.end_date if cas else None,
        enrolment_status=str(student.enrolment_status),
        enrolment_status_reason=student.enrolment_status_reason,
        enrolment_date=student.enrolment_date,
        permission_expiry=student.permission_expiry,
        completion_outcome=student.completion_outcome,
        completion_date=student.completion_date,
        last_status_check=student.last_status_check,
        latest_status_check_outcome=(latest_check.outcome if latest_check else None),
        current_address=student.current_address or {},
        contact_phone=student.contact_phone,
        contact_email=student.contact_email,
        enrolment_evidence=({"id": evidence_doc.id, "filename": evidence_doc.filename} if evidence_doc else None),
        cas_withdrawal_type=cas.withdrawal_type if cas else None,
        cas_withdrawal_date=cas.withdrawal_date if cas else None,
        cas_withdrawal_reason=cas.withdrawal_reason if cas else None,
        visa_outcome=(
            VisaOutcomeOut(
                id=visa_row.id,
                outcome=str(visa_row.outcome),
                decision_date=visa_row.decision_date,
                refusal_reason_category=visa_row.refusal_reason_category,
                recorded_by=recorder.full_name if recorder else None,
                recorded_at=visa_row.recorded_at,
            )
            if visa_row
            else None
        ),
        attendance=AttendanceSummaryOut(
            scheduled=summary.scheduled, attended=summary.attended, present=summary.present,
            authorised_absence=summary.authorised_absence, absent=summary.absent,
            percentage=summary.percentage, threshold=summary.threshold,
            below_threshold=summary.below_threshold,
        ),  # fmt: skip
        attendance_records=[
            AttendanceOut(
                id=r.id, session_date=r.session_date, module=r.module, status=str(r.status),
                absence_reason=r.absence_reason,
                recorded_by=(db.get(User, r.recorded_by).full_name if r.recorded_by else None),
                recorded_at=r.recorded_at,
            )  # fmt: skip
            for r in students_svc.attendance_history(db, student, limit=50)
        ],
        events=[event_out(db, e) for e in events_svc.queue(db, student_id=student.id)],
        escalations=[
            EscalationOut(
                id=e.id, step=e.step, notes=e.notes,
                raised_by=(db.get(User, e.raised_by).full_name if e.raised_by else None),
                raised_at=e.raised_at,
            )  # fmt: skip
            for e in students_svc.escalation_history(db, student)
        ],
    )
