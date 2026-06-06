from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading_system.app.core.enums import DataQualityStatus
from trading_system.app.data.validators.market_data import MarketDataRecord, validate_market_data_record


def _record(**overrides):
    base = {
        "provider": "alpaca_paper",
        "symbol": "AMD",
        "timeframe": "1Min",
        "source_timestamp": datetime(2026, 6, 3, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        "open": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "volume": 100_000,
    }
    base.update(overrides)
    return MarketDataRecord(**base)


def test_valid_market_data_status():
    record = _record()
    result = validate_market_data_record(
        record,
        now=datetime(2026, 6, 3, 10, 1, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result.status == DataQualityStatus.VALID


def test_suspicious_price_status():
    record = _record(high=98.0)
    result = validate_market_data_record(
        record,
        now=datetime(2026, 6, 3, 10, 1, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result.status == DataQualityStatus.SUSPICIOUS_PRICE


def test_out_of_session_status():
    record = _record(source_timestamp=datetime(2026, 6, 3, 2, 0, tzinfo=ZoneInfo("America/New_York")))
    result = validate_market_data_record(
        record,
        now=datetime(2026, 6, 3, 2, 1, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result.status == DataQualityStatus.OUT_OF_SESSION

