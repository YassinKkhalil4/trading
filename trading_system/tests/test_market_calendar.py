from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading_system.app.core.enums import SessionStatus
from trading_system.app.data.market_calendar import get_session, opening_range_window


def test_regular_session_detection():
    ts = datetime(2026, 6, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_session(ts).status == SessionStatus.REGULAR


def test_holiday_detection():
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_session(ts).status == SessionStatus.HOLIDAY


def test_opening_range_window():
    start, end = opening_range_window(datetime(2026, 6, 3).date(), minutes=15)
    assert start.hour == 9
    assert start.minute == 30
    assert end.hour == 9
    assert end.minute == 45

