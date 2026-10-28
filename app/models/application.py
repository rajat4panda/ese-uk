"""applications (LLD 3.2) and documents (LLD 3.3).

applications.status may only be written by the lifecycle executor (LLD 7.2). This is enforced
at run time by an attribute listener, in addition to the code-review rule and the test that
scans the codebase for assignments.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Date, FetchedValue, ForeignKey, Integer, Text, event, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UTCDateTime, created_at_col, pg_enum, pk
from app.models.enums import ApplicationStatus, DocumentCategory, ScanStatus

# Set to True only inside services.lifecycle.transition().
_executor_active: ContextVar[bool] = ContextVar("lifecycle_executor_active", default=False)


class StatusWriteOutsideExecutor(RuntimeError):
    """Raised when code other than the lifecycle executor writes applications.status."""


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = pk()
    ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    applicant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="RESTRICT"))
    owner_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum("application_status"),
        nullable=False,
        server_default=ApplicationStatus.DRAFT.value,
        default=ApplicationStatus.DRAFT,
    )
    programme_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("programmes.id", ondelete="RESTRICT"))
    intake_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("intakes.id", ondelete="RESTRICT"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    # Maintained by trigger; fetched back after every UPDATE for If-Match concurrency (LLD 5.9).
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), server_onupdate=FetchedValue()
    )

    __mapper_args__ = {"eager_defaults": True}

    applicant = relationship("User", foreign_keys=[applicant_id], lazy="joined")
    programme = relationship("Programme", lazy="joined")
    intake = relationship("Intake", lazy="joined")
    documents = relationship(
        "Document",
        back_populates="application",
        order_by="Document.uploaded_at",
        foreign_keys="Document.application_id",
        lazy="selectin",
    )
    academic_assessment = relationship("AcademicAssessment", uselist=False, lazy="selectin")
    compliance_review = relationship("ComplianceReview", uselist=False, lazy="selectin")
    offer = relationship("Offer", uselist=False, lazy="selectin")
    cas = relationship("Cas", uselist=False, lazy="selectin")
    student_record = relationship("StudentRecord", uselist=False, lazy="selectin")
    application_fee = relationship("ApplicationFee", uselist=False, lazy="selectin")
    payments = relationship("Payment", order_by="Payment.payment_date, Payment.id", lazy="selectin")


@event.listens_for(Application.status, "set", propagate=True)
def _guard_status_write(target: Application, value, oldvalue, initiator):  # noqa: ANN001
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.base import NO_VALUE

    state = sa_inspect(target)
    # Allow the column default on a brand-new object (draft), and the executor.
    initial = oldvalue in (NO_VALUE, None) and (state.transient or state.pending)
    if initial and value == ApplicationStatus.DRAFT:
        return
    if not _executor_active.get():
        raise StatusWriteOutsideExecutor(
            "applications.status may only be changed through services.lifecycle.transition() (LLD 7.2)"
        )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes <= 5242880", name="ck_documents_size"),
        CheckConstraint(
            "application_id IS NOT NULL OR student_id IS NOT NULL OR agent_id IS NOT NULL", name="ck_documents_owner"
        ),
    )

    id: Mapped[int] = pk()
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="RESTRICT")
    )
    # FK to student_records(id) is added by the migration that creates student_records (Sprint 4).
    student_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="RESTRICT"))
    category: Mapped[DocumentCategory] = mapped_column(pg_enum("document_category"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=ScanStatus.PENDING.value)
    verified_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expiry_date: Mapped[date | None] = mapped_column(Date)
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("documents.id", ondelete="RESTRICT"))
    uploaded_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("clock_timestamp()")
    )

    application = relationship("Application", back_populates="documents", foreign_keys=[application_id])
