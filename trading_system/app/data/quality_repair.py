from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


MISSING_CANDLE_REPAIR_VERSION = "missing_candle_repair_v1"


@dataclass(frozen=True)
class MissingCandleRepairResult:
    symbols_checked: int
    gaps_detected: int
    repair_attempts: int
    repairs_succeeded: int
    reason: str
    version: str = MISSING_CANDLE_REPAIR_VERSION


class MissingCandleRepairService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def run_once(
        self,
        symbols: list[str] | None = None,
        *,
        provider: str = "alpaca_market_data",
        timeframe: str = "1Min",
        expected_seconds: int = 60,
    ) -> MissingCandleRepairResult:
        symbols_to_check = [symbol.upper() for symbol in (symbols or self.repository.active_symbols())]
        gaps = 0
        attempts = 0
        succeeded = 0
        for symbol in symbols_to_check:
            rows = self.repository.session.scalars(
                select(models.CleanMarketData)
                .where(
                    models.CleanMarketData.symbol == symbol,
                    models.CleanMarketData.provider == provider,
                    models.CleanMarketData.timeframe == timeframe,
                )
                .order_by(desc(models.CleanMarketData.source_timestamp))
                .limit(500)
            ).all()
            ordered = list(reversed(rows))
            previous = None
            for row in ordered:
                if previous and row.source_timestamp and previous.source_timestamp:
                    delta = row.source_timestamp - previous.source_timestamp
                    if delta > timedelta(seconds=expected_seconds * 1.5):
                        gaps += 1
                        attempts += 1
                        repair_result = AlpacaBarsCollector(self.repository, self.settings).collect(
                            symbol,
                            timeframe=timeframe,
                        )
                        repaired = repair_result.success and repair_result.clean_stored > 0
                        succeeded += int(repaired)
                        self.repository.store_missing_candle_gap(
                            provider=provider,
                            symbol=symbol,
                            timeframe=timeframe,
                            previous_timestamp=previous.source_timestamp,
                            current_timestamp=row.source_timestamp,
                            gap_seconds=delta.total_seconds(),
                            repaired=repaired,
                            reason=(
                                "Gap repaired from Alpaca REST bars."
                                if repaired
                                else f"Gap detected but repair failed: {repair_result.reason}"
                            ),
                        )
                previous = row
        return MissingCandleRepairResult(
            symbols_checked=len(symbols_to_check),
            gaps_detected=gaps,
            repair_attempts=attempts,
            repairs_succeeded=succeeded,
            reason="Missing candle repair cycle completed.",
        )
