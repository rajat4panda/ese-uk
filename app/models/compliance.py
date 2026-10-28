"""academic_assessments and compliance_reviews (LLD 3.4).

Structurally parallel, one per application. Both carry completed_by, which the
segregation-of-duties guard depends on (FR-55, LLD 8.4).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import RiskRating

ACADEMIC_OUTCOMES = ("recommend_offer", "request_info", "reject")

#: FR-36. Mirrors the routes the application form offers, plus the case where none is needed.
#: Kept in step with migration 0005's CHECK constraint.
ENGLISH_EVIDENCE = (
    "majority_english_country",
    "degree_taught_in_english",
    "slt_test",
    "provider_assessment",
    "not_required",
)


class _AssessmentBase:
    checklist: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    comments: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    completed_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


class AcademicAssessment(_AssessmentBase, Base):
    __tablename__ = "academic_assessments"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    outcome: Mapped[str | None] = mapped_column(Text)
    # FR-36: which English evidence was accepted, and the standard it was checked against. The
    # checklist tick says the check happened; these say what it found.
    english_evidence: Mapped[str | None] = mapped_column(Text)
    english_standard: Mapped[str | None] = mapped_column(Text)
    # FR-37: the credibility interview, when one is held.
    interview_date: Mapped[date | None] = mapped_column(Date)
    interviewer: Mapped[str | None] = mapped_column(Text)
    interview_outcome: Mapped[str | None] = mapped_column(Text)
    interview_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    completer = relationship("User", foreign_keys="AcademicAssessment.completed_by", lazy="joined")


class ComplianceReview(_AssessmentBase, Base):
    __tablename__ = "compliance_reviews"

    id: Mapped[int] = pk()
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    risk_rating: Mapped[RiskRating | None] = mapped_column(pg_enum("risk_rating"))
    atas_required: Mapped[bool | None] = mapped_column(Boolean)
    atas_reference: Mapped[str | None] = mapped_column(Text)
    # Second-line review for Medium and High risk (FR-52). Columns added in migration 0003.
    second_line_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    second_line_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    second_line_outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    completer = relationship("User", foreign_keys="ComplianceReview.completed_by", lazy="joined")
