from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


UNIVERSE_BUILDER_VERSION = "liquid_universe_builder_v1"
PRODUCTION_UNIVERSE_PROVIDER = "alpaca_market_data"
DAILY_DATA_FRESHNESS_SECONDS = 36 * 60 * 60


@dataclass(frozen=True)
class UniverseRefreshResult:
    symbols_checked: int
    tradable: int
    disabled_or_blocked: int
    reason: str
    version: str = UNIVERSE_BUILDER_VERSION


class LiquidUniverseBuilder:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def refresh(self, symbols: list[str] | None = None) -> UniverseRefreshResult:
        symbols_to_check = [symbol.upper() for symbol in (symbols or self.repository.active_symbols())]
        tradable = 0
        blocked = 0
        for symbol in symbols_to_check:
            reason = self._tradability_reason(symbol)
            is_tradable = reason == "Symbol passes configured liquidity gates."
            if is_tradable:
                tradable += 1
            else:
                blocked += 1
            self.repository.set_symbol_tradability(
                symbol,
                is_tradable=is_tradable,
                reason=reason,
            )
        return UniverseRefreshResult(
            symbols_checked=len(symbols_to_check),
            tradable=tradable,
            disabled_or_blocked=blocked,
            reason="Liquid universe refresh completed from latest clean market data.",
        )

    def _tradability_reason(self, symbol: str) -> str:
        rows = self.repository.session.scalars(
            select(models.CleanMarketData)
            .where(models.CleanMarketData.symbol == symbol.upper())
            .where(models.CleanMarketData.provider == PRODUCTION_UNIVERSE_PROVIDER)
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(20)
        ).all()
        if not rows:
            return "No clean Alpaca market data available for production liquidity gates."
        latest = rows[0]
        if not _timestamp_fresh(
            latest.source_timestamp,
            max_age_seconds=_freshness_seconds_for_timeframe(
                latest.timeframe,
                self.settings.bar_freshness_max_seconds,
            ),
        ):
            return "Clean Alpaca market data is stale for production liquidity gates."
        average_volume = sum(float(row.volume or 0) for row in rows) / max(1, len(rows))
        dollar_volume = average_volume * float(latest.close or 0)
        spread_bps = _spread_proxy_bps(float(latest.low), float(latest.high), float(latest.close))
        if latest.close < self.settings.min_price:
            return f"Latest price {latest.close:.2f} is below minimum price {self.settings.min_price:.2f}."
        if average_volume < self.settings.min_average_volume:
            return (
                f"Average volume {average_volume:.0f} is below minimum "
                f"{self.settings.min_average_volume:.0f}."
            )
        if dollar_volume < self.settings.min_dollar_volume:
            return (
                f"Dollar volume {dollar_volume:.0f} is below minimum "
                f"{self.settings.min_dollar_volume:.0f}."
            )
        if spread_bps > self.settings.max_spread_bps:
            return (
                f"Spread proxy {spread_bps:.1f}bps is above maximum "
                f"{self.settings.max_spread_bps:.1f}bps."
            )
        return "Symbol passes configured liquidity gates."


def _spread_proxy_bps(low: float, high: float, price: float) -> float:
    if price <= 0:
        return 10_000.0
    return max(0.0, (high - low) / price * 10_000)


def _freshness_seconds_for_timeframe(timeframe: str | None, intraday_freshness_seconds: int) -> int:
    if timeframe == "1D":
        return DAILY_DATA_FRESHNESS_SECONDS
    return intraday_freshness_seconds


def _timestamp_fresh(timestamp: datetime | None, *, max_age_seconds: int) -> bool:
    if not timestamp:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - timestamp.astimezone(UTC) <= timedelta(seconds=max_age_seconds)
