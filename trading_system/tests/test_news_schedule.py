from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from trading_system.app.core.config import Settings
from trading_system.app.services.scheduler import news_pull_due

EASTERN = ZoneInfo("America/New_York")


def _utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Build a UTC instant from an Eastern wall-clock time (DST-safe)."""
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN).astimezone(UTC)


def test_premarket_pull_runs_once_per_trading_day():
    settings = Settings()
    # 2026-06-10 is a regular trading Wednesday; 08:00 ET is premarket.
    premarket = _utc(2026, 6, 10, 8, 0)
    due, reason = news_pull_due(premarket, None, settings)
    assert due is True
    assert "Premarket" in reason

    # A second premarket check the same day (after the morning pull) is paused.
    later = _utc(2026, 6, 10, 9, 0)
    due_again, _ = news_pull_due(later, premarket, settings)
    assert due_again is False


def test_premarket_pull_can_be_disabled():
    settings = Settings(scheduler_news_premarket=False)
    premarket = _utc(2026, 6, 10, 8, 0)
    due, _ = news_pull_due(premarket, None, settings)
    assert due is False


def test_intraday_pulls_are_spread_evenly_across_the_session():
    # 13 pulls across the 6.5h session -> a 30-minute interval.
    settings = Settings(scheduler_news_intraday_pulls=13)

    # The first intraday pull with no prior run is due.
    first, _ = news_pull_due(_utc(2026, 6, 10, 10, 0), None, settings)
    assert first is True

    last = _utc(2026, 6, 10, 10, 0)
    # 10 minutes later (< 30-minute interval) -> not yet due.
    soon, _ = news_pull_due(_utc(2026, 6, 10, 10, 10), last, settings)
    assert soon is False
    # 31 minutes later (>= interval) -> due again.
    after, _ = news_pull_due(_utc(2026, 6, 10, 10, 31), last, settings)
    assert after is True


def test_intraday_interval_scales_with_pull_count():
    # Fewer pulls -> wider spacing. 2 pulls across 6.5h -> ~195-minute interval.
    settings = Settings(scheduler_news_intraday_pulls=2)
    last = _utc(2026, 6, 10, 9, 30)
    # One hour after the open is still well inside the first interval.
    due_early, _ = news_pull_due(_utc(2026, 6, 10, 10, 30), last, settings)
    assert due_early is False
    # Three and a half hours later crosses the interval boundary.
    due_late, _ = news_pull_due(_utc(2026, 6, 10, 13, 0), last, settings)
    assert due_late is True


def test_news_is_paused_outside_market_windows():
    settings = Settings()
    # After-hours (17:00 ET), overnight (02:00 ET), and weekend are all paused.
    assert news_pull_due(_utc(2026, 6, 10, 17, 0), None, settings)[0] is False
    assert news_pull_due(_utc(2026, 6, 10, 2, 0), None, settings)[0] is False
    assert news_pull_due(_utc(2026, 6, 13, 11, 0), None, settings)[0] is False
