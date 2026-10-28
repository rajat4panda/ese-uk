"""The working-day reporting clock (LLD 8.2, FR-95, T-12).

The boundary cases the LLD names explicitly: an event on a Friday, an event on a bank holiday, the
Christmas and New Year run, Easter, a year-end crossing, and — the one that matters most — an
unpopulated calendar, which must raise rather than quietly assume there are no holidays.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.services.deadlines import CalendarNotPopulated, reporting_deadline, working_days_between


@contextmanager
def borrowed_calendar(owner_engine: Engine, where: str):
    """Remove part of the bank holiday calendar for one assertion, then put it back exactly."""
    from tests.conftest import BANK_HOLIDAYS

    with owner_engine.connect() as conn:
        conn.execute(text(f"DELETE FROM bank_holidays WHERE {where}"))  # noqa: S608 — test-local literal
        conn.commit()
    try:
        yield
    finally:
        with owner_engine.connect() as conn:
            for holiday_date, name in BANK_HOLIDAYS:
                conn.execute(
                    text("INSERT INTO bank_holidays (holiday_date, name) VALUES (:d, :n) "
                         "ON CONFLICT DO NOTHING"),
                    {"d": holiday_date, "n": name},
                )  # fmt: skip
            conn.commit()


@pytest.fixture
def session(owner_engine: Engine, clean_state) -> Session:
    from app.database import get_sessionmaker

    db = get_sessionmaker()()
    yield db
    db.close()


def test_t12_friday_event_skips_the_weekend(session: Session):
    """Friday 4 September 2026 + 10 working days = Friday 18 September 2026."""
    assert reporting_deadline(session, date(2026, 9, 4)) == date(2026, 9, 18)


def test_t12_christmas_run_skips_both_bank_holidays(session: Session):
    """Friday 18 December 2026: the deadline must step over Christmas Day, the Boxing Day
    substitute and New Year's Day, landing in January rather than on 1 January."""
    deadline = reporting_deadline(session, date(2026, 12, 18))
    # 21-24 Dec (4), 25 Dec and the 28 Dec substitute skipped, 29-31 Dec (7), 1 Jan skipped,
    # 4-6 Jan (10).
    assert deadline == date(2027, 1, 6)
    assert deadline.weekday() < 5
    with_holidays = working_days_between(session, date(2026, 12, 18), deadline)
    assert with_holidays == 10


def test_easter_is_movable_and_still_skipped(session: Session):
    """Good Friday 2027 is 26 March and Easter Monday 29 March; both must be excluded."""
    assert reporting_deadline(session, date(2027, 3, 19)) == date(2027, 4, 6)


def test_event_on_a_bank_holiday_counts_from_the_next_working_day(session: Session):
    """The clock starts the day after the event, so a bank holiday event loses nothing."""
    assert reporting_deadline(session, date(2026, 12, 25)) == date(2027, 1, 12)


def test_year_end_crossing(session: Session):
    assert reporting_deadline(session, date(2027, 12, 20)) == date(2028, 1, 6)


def test_shorter_windows_are_supported(session: Session):
    assert reporting_deadline(session, date(2026, 9, 4), working_days=3) == date(2026, 9, 9)


def test_unpopulated_calendar_raises_rather_than_guessing(session: Session, owner_engine: Engine):
    """A wrong deadline is worse than a loud failure (LLD 8.2).

    The calendar is shared reference data and the application role cannot delete from it, so the
    test borrows it on the owner connection and puts every row back: a test that left holidays
    deleted would make every later deadline wrong.
    """
    with borrowed_calendar(owner_engine, "holiday_date > '2027-01-01'"):
        session.expire_all()
        with pytest.raises(CalendarNotPopulated):
            reporting_deadline(session, date(2027, 6, 1))
    session.expire_all()
    assert reporting_deadline(session, date(2027, 6, 1))  # the calendar is intact again


def test_a_gap_in_the_calendar_is_not_trusted(session: Session, owner_engine: Engine):
    """The last date is not enough: a missing year in the middle must fail too, because a deadline
    computed across it would silently count bank holidays as working days."""
    from app.services.deadlines import calendar_covers

    with borrowed_calendar(owner_engine, "holiday_date BETWEEN '2027-01-01' AND '2027-12-31'"):
        session.expire_all()
        assert calendar_covers(session, date(2028, 6, 1), since=date(2026, 6, 1)) is False
    session.expire_all()
    assert calendar_covers(session, date(2028, 6, 1), since=date(2026, 6, 1)) is True


def test_calendar_headroom_is_reported(session: Session):
    from app.services.deadlines import calendar_headroom

    headroom = calendar_headroom(session)
    assert headroom is not None and headroom > 0


def test_working_days_between_is_the_inverse(session: Session):
    start = date(2026, 9, 4)
    deadline = reporting_deadline(session, start)
    assert working_days_between(session, start, deadline) == 10
    assert working_days_between(session, deadline, start) == -10
