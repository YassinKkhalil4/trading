from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading_system.app.core.enums import SessionStatus


EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)
PREMARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)


@dataclass(frozen=True)
class SessionInfo:
    status: SessionStatus
    session_date: date
    open_at: datetime | None
    close_at: datetime | None
    reason: str


def _observed_date(month: int, day: int, year: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    current = next_month - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_date(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    holidays = {
        _observed_date(1, 1, year),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_date(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_date(6, 19, year),  # Juneteenth
        _observed_date(7, 4, year),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_date(12, 25, year),  # Christmas
    }
    return holidays


def early_close_dates(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        date(year, 12, 24),
        date(year, 7, 3),
    }
    return {item for item in candidates if item.weekday() < 5 and item not in nyse_holidays(year)}


def to_eastern(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(EASTERN)


def is_holiday(session_date: date) -> bool:
    return session_date.weekday() >= 5 or session_date in nyse_holidays(session_date.year)


def is_early_close(session_date: date) -> bool:
    return session_date in early_close_dates(session_date.year)


def regular_session_bounds(session_date: date) -> tuple[datetime, datetime]:
    open_at = datetime.combine(session_date, REGULAR_OPEN, tzinfo=EASTERN)
    close_time = EARLY_CLOSE_TIME if is_early_close(session_date) else REGULAR_CLOSE
    close_at = datetime.combine(session_date, close_time, tzinfo=EASTERN)
    return open_at, close_at


def get_session(timestamp: datetime) -> SessionInfo:
    eastern_ts = to_eastern(timestamp)
    session_date = eastern_ts.date()

    if is_holiday(session_date):
        return SessionInfo(
            status=SessionStatus.HOLIDAY,
            session_date=session_date,
            open_at=None,
            close_at=None,
            reason="NYSE holiday or weekend.",
        )

    open_at, close_at = regular_session_bounds(session_date)
    premarket_open = datetime.combine(session_date, PREMARKET_OPEN, tzinfo=EASTERN)
    after_hours_close = datetime.combine(session_date, AFTER_HOURS_CLOSE, tzinfo=EASTERN)

    if open_at <= eastern_ts <= close_at:
        status = SessionStatus.EARLY_CLOSE if is_early_close(session_date) else SessionStatus.REGULAR
        return SessionInfo(status, session_date, open_at, close_at, "Timestamp is in regular session.")
    if premarket_open <= eastern_ts < open_at:
        return SessionInfo(
            SessionStatus.PREMARKET,
            session_date,
            open_at,
            close_at,
            "Timestamp is in premarket.",
        )
    if close_at < eastern_ts <= after_hours_close:
        return SessionInfo(
            SessionStatus.AFTER_HOURS,
            session_date,
            open_at,
            close_at,
            "Timestamp is in after-hours.",
        )
    return SessionInfo(SessionStatus.CLOSED, session_date, open_at, close_at, "Timestamp is closed.")


def opening_range_window(session_date: date, minutes: int = 15) -> tuple[datetime, datetime]:
    if minutes not in {15, 30}:
        raise ValueError("Only 15-minute and 30-minute opening ranges are supported.")
    open_at, _ = regular_session_bounds(session_date)
    return open_at, open_at + timedelta(minutes=minutes)


def is_stale_session_data(
    source_timestamp: datetime,
    now: datetime | None = None,
    max_age_seconds: int = 120,
) -> bool:
    now = now or datetime.now(UTC)
    if source_timestamp.tzinfo is None:
        source_timestamp = source_timestamp.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - source_timestamp.astimezone(UTC)).total_seconds() > max_age_seconds


def is_regular_or_extended_session(timestamp: datetime) -> bool:
    return get_session(timestamp).status in {
        SessionStatus.REGULAR,
        SessionStatus.EARLY_CLOSE,
        SessionStatus.PREMARKET,
        SessionStatus.AFTER_HOURS,
    }
