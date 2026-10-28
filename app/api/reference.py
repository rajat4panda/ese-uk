"""Reference data for the application form. Any signed-in user.

Addition to LLD 5 (proposed for v1.2): LLD 5.6 exposes programmes and intakes only through
/admin, which applicants cannot reach, yet the wizard must offer them as choices (Appendix A step 4).
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current
from app.database import get_db
from app.models.agent import ATTRIBUTABLE_AGENT_STATUSES, Agent
from app.models.config import Intake, Programme

router = APIRouter(prefix="/reference", tags=["reference data"])


@router.get("/programmes")
def programmes(current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Programme).where(Programme.status == "active").order_by(Programme.title)).all()
    return [{"id": p.id, "title": p.title, "qualification_level": p.qualification_level,
             "duration_months": p.duration_months} for p in rows]  # fmt: skip


@router.get("/intakes")
def intakes(current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Intake)
        .where(Intake.status == "open", Intake.application_deadline >= date.today())
        .order_by(Intake.start_date)
    ).all()
    return [{"id": i.id, "label": i.label, "start_date": i.start_date,
             "application_deadline": i.application_deadline} for i in rows]  # fmt: skip


@router.get("/agents")
def agents(current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Agent).where(Agent.status.in_(ATTRIBUTABLE_AGENT_STATUSES)).order_by(Agent.name)
    ).all()
    return [{"id": a.id, "name": a.name, "country": a.country} for a in rows]


@lru_cache
def _countries() -> list[dict]:
    path = Path(__file__).resolve().parent.parent / "data" / "countries.json"
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/countries")
def countries() -> list[dict]:
    """Public: ISO 3166-1 alpha-2 list used by the form (validation uses the same file)."""
    return _countries()
