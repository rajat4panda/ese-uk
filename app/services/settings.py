"""Run-time compliance settings held in system_settings (LLD 17).

Values are read per request (they are few and cheap), so a change made through the
administration screen takes effect immediately and is audited as admin.config_change.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.config import SystemSetting


def get_setting(db: Session, key: str, default=None):
    row = db.get(SystemSetting, key)
    if row is None:
        return default
    return _coerce(row.value, row.value_type)


def _coerce(value: str, value_type: str):
    if value_type == "int":
        return int(value)
    if value_type == "decimal":
        return Decimal(value)
    if value_type == "bool":
        return value.strip().lower() in {"true", "1", "yes"}
    return value
