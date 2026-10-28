"""Working-day deadline calculation (LLD 8.2, FR-95).

A reportable event must be actioned within 10 working days. Working days exclude weekends and
England-and-Wales bank holidays, which are rows in bank_holidays rather than a hard-coded list,
because the calendar changes (a coronation, a state funeral) and a sponsor cannot wait for a
release to get its deadlines right.

The function raises when the calendar does not reach far enough past the event date. A deadline
that quietly assumed "no holidays" would be wrong in exactly the periods — Christmas, Easter —
where it matters most, and a wrong deadline is worse than a loud failure.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.compliance_events import BankHoliday

# How far past the event date the calendar must reach for the answer to be trustworthy. Ten working
# days can span 40 calendar days only in a pathological run of holidays; the margin is deliberate.
LOOKAHEAD_DAYS = 40

JURISDICTION = "england-and-wales"

# Below this many days of calendar left, the system is weeks away from refusing to calculate a
# deadline at all. The expiry job warns at this point so the calendar is loaded before that.
CALENDAR_HEADROOM_WARNING_DAYS = 120


class CalendarNotPopulated(RuntimeError):
    """The bank holiday calendar does not cover the window this calculation needs."""


def load_holidays(db: Session, start: date, end: date) -> set[date]:
    rows = db.scalars(
        select(BankHoliday.holiday_date).where(
            BankHoliday.holiday_date >= start,
            BankHoliday.holiday_date <= end,
            BankHoliday.jurisdiction == JURISDICTION,
        )
    ).all()
    return set(rows)


def calendar_end(db: Session) -> date | None:
    return db.scalar(
        select(BankHoliday.holiday_date)
        .where(BankHoliday.jurisdiction == JURISDICTION)
        .order_by(BankHoliday.holiday_date.desc())
        .limit(1)
    )


def calendar_covers(db: Session, through: date, *, since: date | None = None) -> bool:
    """Whether the calendar can be trusted from `since` to `through`.

    The last date is not enough on its own: a year loaded with a gap before it would pass that
    check and produce silently wrong deadlines, so every year in the window must have entries.
    England and Wales has at least six bank holidays in every year.
    """
    latest = calendar_end(db)
    if latest is None or latest < through:
        return False
    since = since or date.today()
    for year in range(since.year, through.year + 1):
        count = db.scalar(
            select(func.count())
            .select_from(BankHoliday)
            .where(
                BankHoliday.jurisdiction == JURISDICTION,
                BankHoliday.holiday_date >= date(year, 1, 1),
                BankHoliday.holiday_date <= date(year, 12, 31),
            )
        )
        if not count:
            return False
    return True


def calendar_headroom(db: Session, today: date | None = None) -> int | None:
    """Days of calendar left. None when the table is empty."""
    latest = calendar_end(db)
    return None if latest is None else (latest - (today or date.today())).days


def reporting_deadline(db: Session, event_date: date, working_days: int = 10) -> date:
    """The date by which an event occurring on event_date must be reported."""
    window_end = event_date + timedelta(days=LOOKAHEAD_DAYS)
    if not calendar_covers(db, window_end, since=event_date):
        raise CalendarNotPopulated(
            f"The bank holiday calendar does not reach {window_end.isoformat()}, so the reporting "
            "deadline cannot be calculated. Load the next year of England and Wales bank holidays."
        )
    holidays = load_holidays(db, event_date, window_end)
    day, counted = event_date, 0
    while counted < working_days:
        day += timedelta(days=1)
        if day.weekday() >= 5:  # Saturday, Sunday
            continue
        if day in holidays:
            continue
        counted += 1
    return day


def working_days_between(db: Session, start: date, end: date) -> int:
    """Working days from start (exclusive) to end (inclusive). Negative when end precedes start."""
    if end < start:
        return -working_days_between(db, end, start)
    if not calendar_covers(db, end, since=start):
        raise CalendarNotPopulated(
            f"The bank holiday calendar does not cover {start.isoformat()} to {end.isoformat()}."
        )
    holidays = load_holidays(db, start, end)
    day, counted = start, 0
    while day < end:
        day += timedelta(days=1)
        if day.weekday() >= 5 or day in holidays:
            continue
        counted += 1
    return counted
