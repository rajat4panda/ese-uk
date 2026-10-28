"""SQLAlchemy models, one module per aggregate (LLD 2.1)."""

from app.models.agent import Agent
from app.models.application import Application, Document
from app.models.audit import AuditEntry
from app.models.base import Base
from app.models.cas import Cas, CasAllocation, VisaOutcomeRecord
from app.models.compliance import AcademicAssessment, ComplianceReview
from app.models.compliance_events import AttendanceRecord, BankHoliday, Escalation, ReportableEvent
from app.models.config import (
    ChecklistDefinition,
    Intake,
    OfferConditionLibrary,
    Programme,
    SystemSetting,
)
from app.models.finance import ApplicationFee, FeeSchedule, Payment
from app.models.offer import Offer, OfferCondition
from app.models.operations import ExportJob, JobRun, NotificationQueueRow
from app.models.student import StudentRecord
from app.models.user import EmailVerificationToken, PasswordResetToken, User

__all__ = [
    "AcademicAssessment",
    "Agent",
    "Application",
    "ApplicationFee",
    "AttendanceRecord",
    "AuditEntry",
    "BankHoliday",
    "Base",
    "Cas",
    "CasAllocation",
    "ChecklistDefinition",
    "ComplianceReview",
    "Document",
    "EmailVerificationToken",
    "Escalation",
    "ExportJob",
    "FeeSchedule",
    "Intake",
    "JobRun",
    "Offer",
    "OfferCondition",
    "NotificationQueueRow",
    "OfferConditionLibrary",
    "PasswordResetToken",
    "Payment",
    "Programme",
    "ReportableEvent",
    "StudentRecord",
    "SystemSetting",
    "User",
    "VisaOutcomeRecord",
]
