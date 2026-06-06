from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading_system.app.core.enums import DataQualityStatus, SessionStatus
from trading_system.app.data.market_calendar import get_session, is_stale_session_data


@dataclass(frozen=True)
class MarketDataRecord:
    provider: str
    symbol: str
    timeframe: str
    source_timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class ValidationResult:
    status: DataQualityStatus
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.status == DataQualityStatus.VALID


def validate_market_data_record(
    record: MarketDataRecord,
    *,
    now: datetime | None = None,
    duplicate: bool = False,
    expected_regular_session: bool = False,
    max_age_seconds: int = 120,
) -> ValidationResult:
    if duplicate:
        return ValidationResult(DataQualityStatus.DUPLICATE, "Duplicate provider/symbol/timeframe candle.")

    values = [record.open, record.high, record.low, record.close]
    if any(value <= 0 for value in values):
        return ValidationResult(DataQualityStatus.SUSPICIOUS_PRICE, "OHLC prices must be positive.")
    if record.low > min(record.open, record.close, record.high):
        return ValidationResult(DataQualityStatus.SUSPICIOUS_PRICE, "Low price is above another OHLC value.")
    if record.high < max(record.open, record.close, record.low):
        return ValidationResult(DataQualityStatus.SUSPICIOUS_PRICE, "High price is below another OHLC value.")
    if record.volume < 0:
        return ValidationResult(DataQualityStatus.SUSPICIOUS_VOLUME, "Volume cannot be negative.")

    session = get_session(record.source_timestamp)
    if expected_regular_session and session.status not in {SessionStatus.REGULAR, SessionStatus.EARLY_CLOSE}:
        return ValidationResult(
            DataQualityStatus.OUT_OF_SESSION,
            f"Expected regular session data, got {session.status.value}.",
        )

    if session.status in {SessionStatus.HOLIDAY, SessionStatus.CLOSED}:
        return ValidationResult(DataQualityStatus.OUT_OF_SESSION, session.reason)

    check_now = now or datetime.now(UTC)
    if is_stale_session_data(record.source_timestamp, check_now, max_age_seconds):
        return ValidationResult(DataQualityStatus.STALE, "Candle is older than allowed session freshness.")

    return ValidationResult(DataQualityStatus.VALID, "Market data record passed validation.")


def detect_missing_candle(
    previous_timestamp: datetime | None,
    current_timestamp: datetime,
    timeframe_seconds: int,
) -> ValidationResult:
    if previous_timestamp is None:
        return ValidationResult(DataQualityStatus.VALID, "No previous candle to compare.")
    if previous_timestamp.tzinfo is None:
        previous_timestamp = previous_timestamp.replace(tzinfo=UTC)
    if current_timestamp.tzinfo is None:
        current_timestamp = current_timestamp.replace(tzinfo=UTC)
    gap_seconds = (current_timestamp - previous_timestamp).total_seconds()
    if gap_seconds > timeframe_seconds * 1.5:
        return ValidationResult(
            DataQualityStatus.MISSING,
            f"Missing candle gap detected: {gap_seconds:.0f}s between candles.",
        )
    return ValidationResult(DataQualityStatus.VALID, "No missing candle gap detected.")

