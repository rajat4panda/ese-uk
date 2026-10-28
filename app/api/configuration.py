"""Configuration screen endpoints (BRD FR-105). Rules and audit live in services/configuration.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, require_roles
from app.database import get_db
from app.models.cas import CAS_ASSIGNED, Cas, CasAllocation
from app.schemas.common import StrictModel
from app.services import configuration as svc

router = APIRouter(prefix="/admin", tags=["configuration"])
SETTINGS = tuple(svc.SETTINGS_ROLES)
FEES = tuple(svc.FEE_ROLES)


class SettingIn(StrictModel):
    value: str = Field(max_length=200)


class ProgrammeIn(StrictModel):
    title: str = Field(min_length=3, max_length=200)
    qualification_level: str = Field(min_length=2, max_length=100)
    duration_months: int = Field(ge=1, le=120)
    atas_required: bool = False
    status: Literal["active", "inactive"] = "active"


class ProgrammePatch(StrictModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    qualification_level: str | None = Field(default=None, min_length=2, max_length=100)
    duration_months: int | None = Field(default=None, ge=1, le=120)
    atas_required: bool | None = None
    status: Literal["active", "inactive"] | None = None


class IntakeIn(StrictModel):
    label: str = Field(min_length=3, max_length=100)
    start_date: date
    application_deadline: date
    status: Literal["open", "closed"] = "open"


class IntakePatch(StrictModel):
    label: str | None = Field(default=None, min_length=3, max_length=100)
    start_date: date | None = None
    application_deadline: date | None = None
    status: Literal["open", "closed"] | None = None


class FeeIn(StrictModel):
    programme_id: int
    intake_id: int
    tuition_fee: Decimal = Field(max_digits=10, decimal_places=2)
    minimum_deposit: Decimal = Field(max_digits=10, decimal_places=2)


def _set(model: StrictModel) -> dict:
    return {k: v for k, v in model.model_dump(exclude_unset=True).items() if v is not None}


@router.get("/settings")
def get_settings_list(current: CurrentUser = Depends(require_roles(*SETTINGS)), db: Session = Depends(get_db)):
    return svc.list_settings(db, current.user)


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingIn, request: Request,
                current: CurrentUser = Depends(require_roles(*SETTINGS)),
                db: Session = Depends(get_db)):  # fmt: skip
    out = svc.update_setting(db, current.user, key, body.value, client_ip(request))
    db.commit()
    return out


@router.get("/programmes")
def get_programmes(current: CurrentUser = Depends(require_roles(*SETTINGS)), db: Session = Depends(get_db)):
    return svc.list_programmes(db)


@router.post("/programmes", status_code=201)
def post_programme(body: ProgrammeIn, request: Request,
                   current: CurrentUser = Depends(require_roles(*SETTINGS)),
                   db: Session = Depends(get_db)):  # fmt: skip
    out = svc.save_programme(db, current.user, None, body.model_dump(), client_ip(request))
    db.commit()
    return out


@router.patch("/programmes/{programme_id}")
def patch_programme(programme_id: int, body: ProgrammePatch, request: Request,
                    current: CurrentUser = Depends(require_roles(*SETTINGS)),
                    db: Session = Depends(get_db)):  # fmt: skip
    out = svc.save_programme(db, current.user, programme_id, _set(body), client_ip(request))
    db.commit()
    return out


@router.get("/intakes")
def get_intakes(current: CurrentUser = Depends(require_roles(*SETTINGS)), db: Session = Depends(get_db)):
    return svc.list_intakes(db)


@router.post("/intakes", status_code=201)
def post_intake(body: IntakeIn, request: Request,
                current: CurrentUser = Depends(require_roles(*SETTINGS)),
                db: Session = Depends(get_db)):  # fmt: skip
    out = svc.save_intake(db, current.user, None, body.model_dump(), client_ip(request))
    db.commit()
    return out


@router.patch("/intakes/{intake_id}")
def patch_intake(intake_id: int, body: IntakePatch, request: Request,
                 current: CurrentUser = Depends(require_roles(*SETTINGS)),
                 db: Session = Depends(get_db)):  # fmt: skip
    out = svc.save_intake(db, current.user, intake_id, _set(body), client_ip(request))
    db.commit()
    return out


@router.get("/fees")
def get_fees(current: CurrentUser = Depends(require_roles(*FEES)), db: Session = Depends(get_db)):
    return svc.list_fees(db, current.user)


@router.post("/fees", status_code=201)
def post_fee(body: FeeIn, request: Request,
             current: CurrentUser = Depends(require_roles(*FEES)), db: Session = Depends(get_db)):  # fmt: skip
    out = svc.set_fee(db, current.user, body.programme_id, body.intake_id, body.tuition_fee, body.minimum_deposit,
                      client_ip(request))  # fmt: skip
    db.commit()
    return out


@router.get("/allocations")
def get_allocations(current: CurrentUser = Depends(require_roles(*SETTINGS)),
                    db: Session = Depends(get_db)):  # fmt: skip
    """Allocation periods with their use. Recording one is POST /admin/allocations (workflow.py)."""
    used = select(Cas.allocation_period_id, func.count()).where(Cas.status == CAS_ASSIGNED)
    assigned = dict(db.execute(used.group_by(Cas.allocation_period_id)).all())
    rows = db.scalars(select(CasAllocation).order_by(CasAllocation.period_start.desc())).all()
    return [{"id": r.id, "period_label": r.period_label, "period_start": r.period_start, "period_end": r.period_end,
             "allocated": r.allocated_count, "assigned": int(assigned.get(r.id, 0)),
             "remaining": r.allocated_count - int(assigned.get(r.id, 0))} for r in rows]  # fmt: skip
