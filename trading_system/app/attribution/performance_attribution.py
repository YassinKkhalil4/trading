from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from trading_system.app.core.enums import MarketRegime


PERFORMANCE_ATTRIBUTION_VERSION = "performance_attribution_v1"
UNKNOWN = "UNKNOWN"
EASTERN = ZoneInfo("America/New_York")

HOLDING_PERIOD_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("UNDER_15M", 0.0, 15 * 60),
    ("15M_TO_1H", 15 * 60, 60 * 60),
    ("1H_TO_1D", 60 * 60, 24 * 60 * 60),
    ("OVER_1D", 24 * 60 * 60, float("inf")),
)

TIME_OF_DAY_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("PRE_MARKET", 4 * 60, 9 * 60 + 30),
    ("OPEN", 9 * 60 + 30, 10 * 60 + 30),
    ("MID_MORNING", 10 * 60 + 30, 12 * 60),
    ("MIDDAY", 12 * 60, 14 * 60),
    ("AFTERNOON", 14 * 60, 15 * 60 + 30),
    ("CLOSE", 15 * 60 + 30, 16 * 60),
    ("AFTER_HOURS", 16 * 60, 20 * 60),
)


class JournalTradeRecord(Protocol):
    symbol: str
    strategy_id: str | None
    market_regime: str | None
    catalyst: str | None
    pnl: float | None
    max_adverse_excursion: float | None
    time_in_trade_seconds: float | None
    source_timestamp: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class JournalTrade:
    symbol: str
    strategy_id: str | None = None
    market_regime: str | None = None
    catalyst: str | None = None
    pnl: float | None = None
    max_adverse_excursion: float | None = None
    time_in_trade_seconds: float | None = None
    source_timestamp: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_id: str
    name: str
    version: str = "v1"


@dataclass(frozen=True)
class SymbolSectorInfo:
    sector: str
    industry: str | None = None


@dataclass(frozen=True)
class AttributionGroupMetrics:
    group_key: str
    trade_count: int
    total_pnl: float
    average_r: float | None
    win_rate: float | None
    profit_factor: float | None
    average_hold_time_seconds: float | None
    max_drawdown: float | None


@dataclass(frozen=True)
class PerformanceAttributionResult:
    by_strategy: dict[str, AttributionGroupMetrics]
    by_sector: dict[str, AttributionGroupMetrics]
    by_regime: dict[str, AttributionGroupMetrics]
    by_catalyst_type: dict[str, AttributionGroupMetrics]
    by_time_of_day: dict[str, AttributionGroupMetrics]
    by_holding_period_bucket: dict[str, AttributionGroupMetrics]
    version: str = PERFORMANCE_ATTRIBUTION_VERSION


@dataclass(frozen=True)
class _AttributedTrade:
    strategy_key: str
    sector_key: str
    regime_key: str
    catalyst_key: str
    time_of_day_key: str
    holding_period_key: str
    pnl: float | None
    r_multiple: float | None
    hold_time_seconds: float | None
    sort_timestamp: datetime | None


class PerformanceAttributionService:
    def attribute(
        self,
        *,
        journal_entries: Sequence[JournalTradeRecord],
        strategy_metadata: Mapping[str, StrategyMetadata] | None = None,
        regime_reference: Sequence[str] | None = None,
        catalyst_reference: Mapping[str, str] | None = None,
        symbol_sectors: Mapping[str, SymbolSectorInfo | str] | None = None,
    ) -> PerformanceAttributionResult:
        if not journal_entries:
            return _empty_result()

        strategy_metadata = strategy_metadata or {}
        regime_reference = set(regime_reference or [item.value for item in MarketRegime])
        catalyst_reference = catalyst_reference or {}
        symbol_sectors = symbol_sectors or {}

        trades = [
            self._attribute_trade(
                entry,
                strategy_metadata=strategy_metadata,
                regime_reference=regime_reference,
                catalyst_reference=catalyst_reference,
                symbol_sectors=symbol_sectors,
            )
            for entry in journal_entries
        ]
        return PerformanceAttributionResult(
            by_strategy=_aggregate(trades, lambda trade: trade.strategy_key),
            by_sector=_aggregate(trades, lambda trade: trade.sector_key),
            by_regime=_aggregate(trades, lambda trade: trade.regime_key),
            by_catalyst_type=_aggregate(trades, lambda trade: trade.catalyst_key),
            by_time_of_day=_aggregate(trades, lambda trade: trade.time_of_day_key),
            by_holding_period_bucket=_aggregate(trades, lambda trade: trade.holding_period_key),
        )

    def _attribute_trade(
        self,
        entry: JournalTradeRecord,
        *,
        strategy_metadata: Mapping[str, StrategyMetadata],
        regime_reference: set[str],
        catalyst_reference: Mapping[str, str],
        symbol_sectors: Mapping[str, SymbolSectorInfo | str],
    ) -> _AttributedTrade:
        timestamp = entry.source_timestamp or entry.created_at
        return _AttributedTrade(
            strategy_key=_strategy_key(entry.strategy_id, strategy_metadata),
            sector_key=_sector_key(entry.symbol, symbol_sectors),
            regime_key=_regime_key(entry.market_regime, regime_reference),
            catalyst_key=_catalyst_key(entry.catalyst, catalyst_reference),
            time_of_day_key=_time_of_day_bucket(timestamp),
            holding_period_key=_holding_period_bucket(entry.time_in_trade_seconds),
            pnl=entry.pnl,
            r_multiple=_r_multiple(entry.pnl, entry.max_adverse_excursion),
            hold_time_seconds=entry.time_in_trade_seconds,
            sort_timestamp=timestamp,
        )


def _empty_result() -> PerformanceAttributionResult:
    return PerformanceAttributionResult(
        by_strategy={},
        by_sector={},
        by_regime={},
        by_catalyst_type={},
        by_time_of_day={},
        by_holding_period_bucket={},
    )


def _strategy_key(
    strategy_id: str | None,
    strategy_metadata: Mapping[str, StrategyMetadata],
) -> str:
    if not strategy_id:
        return UNKNOWN
    metadata = strategy_metadata.get(strategy_id)
    if metadata is not None:
        return metadata.strategy_id
    return strategy_id


def _sector_key(symbol: str, symbol_sectors: Mapping[str, SymbolSectorInfo | str]) -> str:
    info = symbol_sectors.get(symbol.upper())
    if info is None:
        return UNKNOWN
    if isinstance(info, SymbolSectorInfo):
        return info.sector or UNKNOWN
    return info or UNKNOWN


def _regime_key(market_regime: str | None, regime_reference: set[str]) -> str:
    if not market_regime:
        return UNKNOWN
    normalized = market_regime.strip().upper()
    if normalized in regime_reference:
        return normalized
    return UNKNOWN


def _catalyst_key(catalyst: str | None, catalyst_reference: Mapping[str, str]) -> str:
    if not catalyst:
        return UNKNOWN
    normalized = catalyst.strip()
    if normalized in catalyst_reference:
        return catalyst_reference[normalized]
    upper = normalized.upper()
    if upper in {value.upper() for value in catalyst_reference.values()}:
        return upper
    if normalized in catalyst_reference.values():
        return normalized
    return UNKNOWN


def _time_of_day_bucket(timestamp: datetime | None) -> str:
    if timestamp is None:
        return UNKNOWN
    localized = _as_utc(timestamp).astimezone(EASTERN)
    minute_of_day = localized.hour * 60 + localized.minute
    for label, start_minute, end_minute in TIME_OF_DAY_BUCKETS:
        if start_minute <= minute_of_day < end_minute:
            return label
    return UNKNOWN


def _holding_period_bucket(time_in_trade_seconds: float | None) -> str:
    if time_in_trade_seconds is None:
        return UNKNOWN
    seconds = max(0.0, float(time_in_trade_seconds))
    for label, lower, upper in HOLDING_PERIOD_BUCKETS:
        if lower <= seconds < upper:
            return label
    return UNKNOWN


def _r_multiple(pnl: float | None, max_adverse_excursion: float | None) -> float | None:
    if pnl is None or max_adverse_excursion is None:
        return None
    if max_adverse_excursion >= 0:
        return None
    risk = abs(max_adverse_excursion)
    if risk == 0:
        return None
    return pnl / risk


def _aggregate(
    trades: Sequence[_AttributedTrade],
    key_fn,
) -> dict[str, AttributionGroupMetrics]:
    grouped: dict[str, list[_AttributedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[key_fn(trade)].append(trade)

    return {
        group_key: _metrics_for_group(group_key, group_trades)
        for group_key, group_trades in sorted(grouped.items())
    }


def _metrics_for_group(group_key: str, trades: Sequence[_AttributedTrade]) -> AttributionGroupMetrics:
    pnls = [trade.pnl for trade in trades if trade.pnl is not None]
    r_values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    hold_times = [trade.hold_time_seconds for trade in trades if trade.hold_time_seconds is not None]
    total_pnl = sum(pnls) if pnls else 0.0
    return AttributionGroupMetrics(
        group_key=group_key,
        trade_count=len(trades),
        total_pnl=total_pnl,
        average_r=(sum(r_values) / len(r_values)) if r_values else None,
        win_rate=_win_rate(pnls),
        profit_factor=_profit_factor(pnls),
        average_hold_time_seconds=(sum(hold_times) / len(hold_times)) if hold_times else None,
        max_drawdown=_max_drawdown(trades),
    )


def _win_rate(pnls: Sequence[float]) -> float | None:
    if not pnls:
        return None
    return sum(1 for pnl in pnls if pnl > 0) / len(pnls)


def _profit_factor(pnls: Sequence[float]) -> float | None:
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def _max_drawdown(trades: Sequence[_AttributedTrade]) -> float | None:
    ordered = sorted(
        (trade for trade in trades if trade.pnl is not None),
        key=lambda trade: trade.sort_timestamp or datetime.min.replace(tzinfo=UTC),
    )
    if len(ordered) < 2:
        return None
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in ordered:
        cumulative += float(trade.pnl)
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return max_drawdown


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
