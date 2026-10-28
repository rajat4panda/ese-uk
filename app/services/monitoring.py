"""Who is compliant, who needs attention, who is critical (BRD FR-70, FR-71; prototype screen 13).

The screens used to decide this in the browser from one thing only — open reportable events — so a
student with 40% attendance, or with a visa about to lapse, showed as "compliant" until somebody
raised an event by hand. Classification now lives here, once, on the server, and every flag carries
the reason in words so the list can say *why* a student is on it.

Rules, each read from the configured settings at the moment of asking:

  critical   a reportable event past its UKVI deadline
             permission to stay already expired while the student is still sponsored
             the latest right-to-study check found a discrepancy or no valid permission (FR-68)
  attention  a reportable event open and inside its deadline
             attendance over the last 30 days below ATTENDANCE_THRESHOLD_PCT
             permission to stay expiring within PERMISSION_EXPIRY_ALERT_DAYS
             on course with no right-to-study check on record (FR-68)
             enrolled with no evidence of enrolment on file (FR-61)
             on course with no UK address on record (FR-67)

Who is counted: every student whose record is still open (students.CLOSED_STATUSES), and also any
closed record that still has an event waiting to be reported. The events that matter most —
withdrawal, curtailment, non-enrolment — are raised at the very moment the record closes, so leaving
closed records out would hide exactly the reports most likely to be missed.

Queries are batched — one for open events, one for recent attendance, one for the applications —
so the cost does not grow by a query per student.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.application import Application
from app.models.compliance_events import EVENT_PENDING, AttendanceRecord, ReportableEvent
from app.models.enums import AttendanceStatus, EnrolmentStatus
from app.models.student import StatusCheck, StudentRecord
from app.services.settings import get_setting
from app.services.students import CLOSED_STATUSES

ON_COURSE = (EnrolmentStatus.ENROLLED, EnrolmentStatus.INTERRUPTED)
ENGAGEMENT_WINDOW_DAYS = 30  # the same window the engagement_check job uses
SEVERITY_ORDER = {"critical": 2, "attention": 1, "compliant": 0}

EVENT_LABELS = {
    "non_enrolment": "Non-enrolment", "withdrawal": "Withdrawal", "deferral": "Deferral",
    "course_change": "Change of course", "curtailment": "Curtailment", "interruption": "Interruption",
    "change_of_circumstances": "Change of circumstances", "cas_withdrawal": "CAS withdrawal",
}  # fmt: skip


@dataclass
class Issue:
    # event_overdue | event_open | attendance | visa_expired | visa_expiring | status_check_failed |
    # status_check_missing | enrolment_evidence_missing | address_update
    kind: str
    severity: str  # critical | attention
    label: str
    since: date | None
    detail: str


@dataclass
class _Attendance:
    scheduled: int = 0
    attended: int = 0


def _issues(student: StudentRecord, events: list[ReportableEvent], attendance: _Attendance | None, *,
            today: date, threshold: int, notice_days: int,
            latest_check: StatusCheck | None = None) -> list[Issue]:  # fmt: skip
    out: list[Issue] = []
    for e in events:
        name = EVENT_LABELS.get(str(e.event_type), str(e.event_type).replace("_", " ").capitalize())
        if e.deadline_date < today:
            out.append(Issue("event_overdue", "critical", f"{name} report overdue", e.deadline_date,
                             f"Deadline was {e.deadline_date.isoformat()}"))  # fmt: skip
        else:
            out.append(Issue("event_open", "attention", f"{name} to report", e.event_date,
                             f"Report to UKVI by {e.deadline_date.isoformat()}"))  # fmt: skip

    if student.enrolment_status in ON_COURSE and attendance and attendance.scheduled:
        pct = round(100.0 * attendance.attended / attendance.scheduled, 1)
        if pct < threshold:
            out.append(Issue("attendance", "attention", "Engagement concern",
                             today - timedelta(days=ENGAGEMENT_WINDOW_DAYS),
                             f"Attendance {pct}% over the last {ENGAGEMENT_WINDOW_DAYS} days, "
                             f"below {threshold}%"))  # fmt: skip

    if student.permission_expiry is not None and student.enrolment_status not in CLOSED_STATUSES:
        if student.permission_expiry < today:
            out.append(Issue("visa_expired", "critical", "Permission to stay expired", student.permission_expiry,
                             f"Expired {student.permission_expiry.isoformat()}"))  # fmt: skip
        elif student.permission_expiry <= today + timedelta(days=notice_days):
            out.append(Issue("visa_expiring", "attention", "Visa expiry approaching", student.permission_expiry,
                             f"Expires {student.permission_expiry.isoformat()}"))  # fmt: skip

    if student.enrolment_status in ON_COURSE:
        if latest_check is None:
            out.append(Issue("status_check_missing", "attention", "Right-to-study check needed", student.enrolment_date,
                             "No right-to-study check is on record"))  # fmt: skip
        elif latest_check.outcome != "confirmed":
            found = "a discrepancy" if latest_check.outcome == "discrepancy" else "no valid permission"
            out.append(Issue("status_check_failed", "critical", "Right-to-study check failed", latest_check.check_date,
                             f"Check on {latest_check.check_date.isoformat()} found {found}"))  # fmt: skip
        address = student.current_address or {}
        if not address.get("line1") or (address.get("country") or "").upper() != "GB":
            out.append(Issue("address_update", "attention", "Address update required", student.enrolment_date,
                             "No UK address is on record"))  # fmt: skip
    if student.enrolment_status == EnrolmentStatus.ENROLLED and student.enrolment_evidence_id is None:
        out.append(Issue("enrolment_evidence_missing", "attention", "Enrolment evidence missing",
                         student.enrolment_date, "Enrolled with no evidence of enrolment on file"))  # fmt: skip
    return out


def overview(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    threshold = int(get_setting(db, "ATTENDANCE_THRESHOLD_PCT", 80))
    notice_days = int(get_setting(db, "PERMISSION_EXPIRY_ALERT_DAYS", 60))

    pending = db.scalars(select(ReportableEvent).where(ReportableEvent.status == EVENT_PENDING)).all()
    events_by_student: dict[int, list[ReportableEvent]] = defaultdict(list)
    for e in pending:
        events_by_student[e.student_id].append(e)

    population = db.scalars(select(StudentRecord).where(or_(
        StudentRecord.enrolment_status.not_in(CLOSED_STATUSES),
        StudentRecord.id.in_(list(events_by_student) or [-1]),
    ))).all()  # fmt: skip

    since = today - timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    attendance: dict[int, _Attendance] = defaultdict(_Attendance)
    rows = db.execute(
        select(AttendanceRecord.student_id, AttendanceRecord.status, func.count())
        .where(AttendanceRecord.session_date >= since)
        .group_by(AttendanceRecord.student_id, AttendanceRecord.status)
    ).all()
    for student_id, status, n in rows:
        attendance[student_id].scheduled += int(n)
        if status in (AttendanceStatus.PRESENT, AttendanceStatus.AUTHORISED_ABSENCE):
            attendance[student_id].attended += int(n)

    # The latest check per student, in one query: DISTINCT ON keeps the first row of each student.
    latest_checks = {c.student_id: c for c in db.scalars(
        select(StatusCheck).distinct(StatusCheck.student_id)
        .where(StatusCheck.student_id.in_([s.id for s in population] or [-1]))
        .order_by(StatusCheck.student_id, StatusCheck.check_date.desc(), StatusCheck.id.desc())
    )}  # fmt: skip

    flagged_rows = []
    for s in population:
        issues = _issues(s, events_by_student.get(s.id, []), attendance.get(s.id), today=today,
                         threshold=threshold, notice_days=notice_days,
                         latest_check=latest_checks.get(s.id))  # fmt: skip
        if issues:
            flagged_rows.append((s, issues))

    app_ids = [s.application_id for s, _ in flagged_rows] or [-1]
    app_query = select(Application).options(selectinload(Application.programme)).where(Application.id.in_(app_ids))
    apps = {a.id: a for a in db.scalars(app_query)}
    counts = {"critical": 0, "attention": 0}
    kinds: dict[str, int] = {}
    flagged = []
    for s, issues in flagged_rows:
        issues.sort(key=lambda i: -SEVERITY_ORDER[i.severity])
        level = issues[0].severity
        counts[level] += 1
        for i in issues:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        app = apps.get(s.application_id)
        flagged.append({
            "student_id": s.id, "application_ref": app.ref if app else "—",
            "student_name": app.data.get("full_name") if app else None,
            "programme": app.programme.title if app and app.programme else None,
            "enrolment_status": str(s.enrolment_status), "severity": level,
            "issues": [asdict(i) for i in issues],
        })  # fmt: skip
    flagged.sort(key=lambda r: (-SEVERITY_ORDER[r["severity"]], r["student_name"] or ""))
    return {
        "as_of": today, "total": len(population), "compliant": len(population) - len(flagged),
        "attention": counts["attention"], "critical": counts["critical"], "issue_counts": kinds,
        "students": flagged,
    }  # fmt: skip
