"""Basic Compliance Assessment metrics (LLD 8.3, FR-87 to FR-90).

These are the three numbers UKVI judges a sponsor on. Two design points matter:

  * Each metric is computed against the CAS population for the period, matching the basis UKVI
    uses — not against enrolled students, which would quietly exclude the refusals and
    non-enrolments that are the whole point of the measure.
  * The overall rating is the worst of the three, never an average. A sponsor with one red metric
    is in trouble regardless of how good the other two look.

Under a denominator of 20, the absolute counts are returned alongside the percentage, so the
interface can say "1 refusal in 7" rather than presenting 14.3% as if it were a stable rate (FR-90).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.cas import CAS_ASSIGNED, Cas, VisaOutcomeRecord
from app.models.enums import EnrolmentStatus, VisaOutcome
from app.models.student import StudentRecord
from app.services.settings import get_setting

SMALL_DENOMINATOR = 20  # FR-90

GREEN, AMBER, RED = "green", "amber", "red"
RAG_ORDER = {GREEN: 0, AMBER: 1, RED: 2}


@dataclass
class Metric:
    key: str
    label: str
    numerator: int
    denominator: int
    threshold: float
    direction: str  # "max" — lower is better (refusals); "min" — higher is better
    percentage: float | None = None
    rag: str = GREEN
    small_sample: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "percentage": self.percentage,
            "threshold": self.threshold,
            "direction": self.direction,
            "rag": self.rag,
            "small_sample": self.small_sample,
            "note": self.note,
        }


@dataclass
class MetricSet:
    period_start: date
    period_end: date
    metrics: list[Metric] = field(default_factory=list)
    overall_rag: str = GREEN

    def to_dict(self) -> dict:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "metrics": [m.to_dict() for m in self.metrics],
            "overall_rag": self.overall_rag,
        }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _rag(metric: Metric, amber_band: float) -> str:
    if metric.percentage is None:
        return GREEN  # nothing measured yet is not a breach
    if metric.direction == "max":
        if metric.percentage <= metric.threshold:
            return GREEN
        return AMBER if metric.percentage <= metric.threshold + amber_band else RED
    if metric.percentage >= metric.threshold:
        return GREEN
    return AMBER if metric.percentage >= metric.threshold - amber_band else RED


def default_period(today: date | None = None) -> tuple[date, date]:
    """The rolling 12 months to today — the window a sponsor is usually assessed over. The end is
    exclusive, so today's own assignments count in today's period and nowhere else."""
    today = today or date.today()
    start = date(today.year - 1, today.month, 1)
    return start, today + timedelta(days=1)


def compute(db: Session, period_start: date | None = None, period_end: date | None = None) -> MetricSet:
    start, end = default_period()
    period_start = period_start or start
    period_end = period_end or end
    # Half-open, as in LLD 8.3: a CAS assigned on the boundary belongs to one period, not two.
    end_exclusive = datetime.combine(period_end, datetime.min.time())

    amber_band = float(Decimal(str(get_setting(db, "BCA_AMBER_BAND_PCT", 2.0))))
    refusal_max = float(Decimal(str(get_setting(db, "BCA_REFUSAL_MAX_PCT", 5.0))))
    enrolment_min = float(Decimal(str(get_setting(db, "BCA_ENROLMENT_MIN_PCT", 95.0))))
    completion_min = float(Decimal(str(get_setting(db, "BCA_COMPLETION_MIN_PCT", 85.0))))

    in_period = (Cas.assigned_at >= datetime.combine(period_start, datetime.min.time()),
                 Cas.assigned_at < end_exclusive)  # fmt: skip

    # Refusal rate: refusals / CAS assigned in the period (LLD 8.3).
    refusal_total = db.scalar(
        select(func.count()).select_from(Cas).where(*in_period, Cas.status == CAS_ASSIGNED)
    ) or 0
    refusals = db.scalar(
        select(func.count())
        .select_from(Cas)
        .join(VisaOutcomeRecord, VisaOutcomeRecord.cas_id == Cas.id)
        .where(*in_period, Cas.status == CAS_ASSIGNED, VisaOutcomeRecord.outcome == VisaOutcome.REFUSED)
    ) or 0

    # Enrolment rate: enrolled or completed / CAS assigned in the period.
    enrolment_total = db.scalar(select(func.count()).select_from(Cas).where(*in_period)) or 0
    enrolled = db.scalar(
        select(func.count())
        .select_from(Cas)
        .join(StudentRecord, StudentRecord.cas_id == Cas.id)
        .where(
            *in_period,
            StudentRecord.enrolment_status.in_([EnrolmentStatus.ENROLLED, EnrolmentStatus.COMPLETED]),
        )
    ) or 0

    # Completion rate: completed / expected to complete in the period (by course end date).
    completion_total = db.scalar(
        select(func.count())
        .select_from(StudentRecord)
        .join(Cas, Cas.id == StudentRecord.cas_id)
        .where(Cas.end_date >= period_start, Cas.end_date < period_end)
    ) or 0
    completed = db.scalar(
        select(func.count())
        .select_from(StudentRecord)
        .join(Cas, Cas.id == StudentRecord.cas_id)
        .where(Cas.end_date >= period_start, Cas.end_date < period_end,
               StudentRecord.completion_outcome == "completed")  # fmt: skip
    ) or 0

    metrics = [
        Metric("refusal", "Visa refusal rate", int(refusals), int(refusal_total), refusal_max, "max"),
        Metric("enrolment", "Enrolment rate", int(enrolled), int(enrolment_total), enrolment_min, "min"),
        Metric("completion", "Course completion rate", int(completed), int(completion_total), completion_min, "min"),
    ]
    for metric in metrics:
        metric.percentage = _rate(metric.numerator, metric.denominator)
        metric.rag = _rag(metric, amber_band)
        metric.small_sample = 0 < metric.denominator < SMALL_DENOMINATOR
        if metric.denominator == 0:
            metric.note = "No cases in this period."
        elif metric.small_sample:
            metric.note = (
                f"{metric.numerator} of {metric.denominator} — too few cases for the percentage to be "
                "stable, so read the counts."
            )

    overall = max((m.rag for m in metrics), key=lambda r: RAG_ORDER[r], default=GREEN)
    return MetricSet(period_start=period_start, period_end=period_end, metrics=metrics, overall_rag=overall)
