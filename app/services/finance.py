"""Fee position, fee schedules and payments (LLD 3.9, 8.5, 8.6).

The balance is derived from the payment history on every read, never stored, so it cannot drift
from the evidence (LLD 3.9). The deposit gate reads the same function the fees screen shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import GuardFailed, NotFound
from app.models.application import Application
from app.models.enums import PaymentType, UserRole
from app.models.finance import ApplicationFee, FeeSchedule, Payment
from app.models.user import User
from app.services import audit

ZERO = Decimal("0.00")
FINANCE_ROLES = frozenset({UserRole.FINANCE, UserRole.SENIOR, UserRole.ADMIN})
AUDIT_ACTION = {
    PaymentType.PAYMENT: "finance.payment",
    PaymentType.DEPOSIT: "finance.payment",
    PaymentType.SCHOLARSHIP: "finance.scholarship",
    PaymentType.REFUND: "finance.refund",
}


@dataclass(frozen=True)
class FeePosition:
    agreed_fee: Decimal | None
    minimum_deposit: Decimal | None
    paid: Decimal
    scholarship: Decimal
    refunded: Decimal
    outstanding: Decimal | None
    deposit_met: bool
    currency: str = "GBP"

    @property
    def fee_set(self) -> bool:
        return self.agreed_fee is not None


def current_schedule(db: Session, programme_id: int | None, intake_id: int | None) -> FeeSchedule | None:
    if not programme_id or not intake_id:
        return None
    return db.scalars(
        select(FeeSchedule).where(
            FeeSchedule.programme_id == programme_id,
            FeeSchedule.intake_id == intake_id,
            FeeSchedule.effective_to.is_(None),
        )
    ).first()


def fee_position(db: Session, app: Application) -> FeePosition:
    """LLD 8.6. Amounts are stored positive; the type decides the sign."""
    payments = list(app.payments)
    paid = sum((p.amount for p in payments if p.type in (PaymentType.PAYMENT, PaymentType.DEPOSIT)), ZERO)
    scholarship = sum((p.amount for p in payments if p.type == PaymentType.SCHOLARSHIP), ZERO)
    refunded = sum((p.amount for p in payments if p.type == PaymentType.REFUND), ZERO)

    fee_row: ApplicationFee | None = app.application_fee
    schedule = fee_row.schedule if fee_row else current_schedule(db, app.programme_id, app.intake_id)
    agreed = fee_row.agreed_fee if fee_row else (schedule.tuition_fee if schedule else None)
    minimum = schedule.minimum_deposit if schedule else None
    outstanding = None if agreed is None else agreed - scholarship - paid + refunded
    deposit_met = minimum is not None and (paid - refunded) >= minimum
    return FeePosition(
        agreed_fee=agreed,
        minimum_deposit=minimum,
        paid=paid,
        scholarship=scholarship,
        refunded=refunded,
        outstanding=outstanding,
        deposit_met=deposit_met,
        currency=schedule.currency if schedule else "GBP",
    )


def attach_fee(db: Session, app: Application, actor: User) -> ApplicationFee | None:
    """Fix the fee schedule that applies to this application, at offer time (LLD 3.9)."""
    if app.application_fee is not None:
        return app.application_fee
    schedule = current_schedule(db, app.programme_id, app.intake_id)
    if schedule is None:
        return None
    row = ApplicationFee(
        application_id=app.id, fee_schedule_id=schedule.id, agreed_fee=schedule.tuition_fee, set_by=actor.id
    )
    db.add(row)
    db.flush()
    audit.write(
        db, actor, "finance.schedule_change", application=app, entity_type="application_fee", entity_id=row.id,
        detail=f"Fee set from schedule {schedule.id}: {schedule.currency} {schedule.tuition_fee} "
               f"(minimum deposit {schedule.minimum_deposit})",
    )  # fmt: skip
    return row


def record_payment(
    db: Session,
    app: Application,
    actor: User,
    *,
    payment_type: PaymentType,
    amount: Decimal,
    payment_date: date,
    method: str | None = None,
    reference: str | None = None,
    reason: str | None = None,
    ip: str | None = None,
) -> Payment:
    if actor.role not in FINANCE_ROLES:
        raise GuardFailed("ERR_ROLE_FORBIDDEN", "Only finance, senior or administrator users may record payments.",
                          status_code=403)  # fmt: skip
    if amount <= ZERO:
        raise GuardFailed("ERR_INVALID_AMOUNT", "Enter an amount greater than zero.",
                          details=[{"field": "amount", "code": "ERR_INVALID_AMOUNT"}])  # fmt: skip
    if payment_type in (PaymentType.SCHOLARSHIP, PaymentType.REFUND) and not reason:
        raise GuardFailed("ERR_REASON_REQUIRED", "A reason is required for a scholarship or refund.",
                          details=[{"field": "reason", "code": "ERR_REASON_REQUIRED"}])  # fmt: skip
    if payment_date > date.today():
        raise GuardFailed("ERR_DATE_RANGE", "The payment date cannot be in the future.",
                          details=[{"field": "payment_date", "code": "ERR_DATE_RANGE"}])  # fmt: skip

    payment = Payment(
        application_id=app.id, type=payment_type, amount=amount, payment_date=payment_date, method=method,
        reference=reference, reason=reason, authorised_by=actor.id,
    )  # fmt: skip
    db.add(payment)
    db.flush()
    audit.write(
        db, actor, AUDIT_ACTION[payment_type], application=app, entity_type="payment", entity_id=payment.id,
        detail=f"{payment_type.value} of GBP {amount} on {payment_date}"
               + (f" ({reference})" if reference else "") + (f" — {reason}" if reason else ""),
        ip=ip,
    )  # fmt: skip
    return payment


def get_application_for_finance(db: Session, application_id: int) -> Application:
    app = db.get(Application, application_id)
    if app is None:
        raise NotFound()
    return app
