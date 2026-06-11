"""Expectancy Layer.

For every ranked opportunity this module answers: "When trades looked like this in
the past, what actually happened?" It derives outcome statistics (win rate, R-multiple
distribution, drawdown, time-to-target, early-failure rate) from REAL closed trades only.

Correctness notes (see architect plan):
- A trade is "closed" only when fully exited. Closed status and the exact entry/exit
  timestamps come from ``Fill`` joined to ``Order`` grouped by ``signal_id`` + side -- NOT
  from ``TradeJournal`` (whose ``pnl``/``time_in_trade_seconds`` also reflect partial
  exits and whose ``source_timestamp`` is row-creation time, not the entry fill time).
- R-multiple is price based so it never needs a stored position size:
  long  R = (exit - entry) / (entry - stop); short R = (entry - exit) / (stop - entry).
  R is ``None`` when there is no stop or the risk denominator is <= 0.
- Max drawdown is computed on the cumulative-R curve (basis ``"R"``) and only falls back
  to the pnl curve (basis ``"pnl"``) when no trade has a computable R.
- ``failure_rate_before_1030`` counts losing trades whose exit fill, in America/New_York,
  lands before 10:30; the denominator is the full closed-trade sample.

When no trades match, every statistic is ``None`` and ``sample_size`` is ``0`` -- this
layer never fabricates numbers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from trading_system.app.core.enums import Direction
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.execution.order_side import entry_side_from_direction

EXPECTANCY_RULE_VERSION = "expectancy_v1"

_EASTERN = ZoneInfo("America/New_York")
_EARLY_FAILURE_CUTOFF = time(hour=10, minute=30)
UNKNOWN_BUCKET = "UNKNOWN"
NO_CATALYST_BUCKET = "NO_CATALYST"

TIME_OF_DAY_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("PRE_MARKET", 4 * 60, 9 * 60 + 30),
    ("OPEN", 9 * 60 + 30, 10 * 60 + 30),
    ("MID_MORNING", 10 * 60 + 30, 12 * 60),
    ("MIDDAY", 12 * 60, 14 * 60),
    ("AFTERNOON", 14 * 60, 15 * 60 + 30),
    ("CLOSE", 15 * 60 + 30, 16 * 60),
    ("AFTER_HOURS", 16 * 60, 20 * 60),
)
RELATIVE_VOLUME_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("UNDER_1X", 0.0, 1.0),
    ("1X_TO_1_5X", 1.0, 1.5),
    ("1_5X_TO_2X", 1.5, 2.0),
    ("2X_TO_3X", 2.0, 3.0),
    ("OVER_3X", 3.0, float("inf")),
)
SPREAD_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("TIGHT_UNDER_5_BPS", 0.0, 5.0),
    ("5_TO_10_BPS", 5.0, 10.0),
    ("10_TO_20_BPS", 10.0, 20.0),
    ("WIDE_OVER_20_BPS", 20.0, float("inf")),
)
VOLATILITY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("LOW_UNDER_25", 0.0, 25.0),
    ("MODERATE_25_TO_50", 25.0, 50.0),
    ("HIGH_50_TO_75", 50.0, 75.0),
    ("EXTREME_OVER_75", 75.0, float("inf")),
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)



def _numeric_bucket(value: float | None, buckets: Sequence[tuple[str, float, float]]) -> str:
    if value is None:
        return UNKNOWN_BUCKET
    numeric = max(0.0, float(value))
    for label, lower, upper in buckets:
        if lower <= numeric < upper:
            return label
    return UNKNOWN_BUCKET


def time_of_day_bucket(timestamp: datetime | None) -> str:
    if timestamp is None:
        return UNKNOWN_BUCKET
    localized = _as_utc(timestamp).astimezone(_EASTERN)
    minute_of_day = localized.hour * 60 + localized.minute
    for label, start_minute, end_minute in TIME_OF_DAY_BUCKETS:
        if start_minute <= minute_of_day < end_minute:
            return label
    return UNKNOWN_BUCKET


def relative_volume_bucket(relative_volume: float | None) -> str:
    return _numeric_bucket(relative_volume, RELATIVE_VOLUME_BUCKETS)


def spread_bucket(spread_bps: float | None) -> str:
    return _numeric_bucket(spread_bps, SPREAD_BUCKETS)


def volatility_bucket(volatility_score: float | None) -> str:
    return _numeric_bucket(volatility_score, VOLATILITY_BUCKETS)


def _float_from_mapping(payload: dict | None, *path: str) -> float | None:
    current: object = payload or {}
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _first_float(payload: dict | None, paths: Sequence[tuple[str, ...]]) -> float | None:
    for path in paths:
        value = _float_from_mapping(payload, *path)
        if value is not None:
            return value
    return None


def _str_from_mapping(payload: dict | None, *path: str) -> str | None:
    current: object = payload or {}
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current in (None, ""):
        return None
    return str(current)


def _first_str(payload: dict | None, paths: Sequence[tuple[str, ...]]) -> str | None:
    for path in paths:
        value = _str_from_mapping(payload, *path)
        if value:
            return value
    return None


def _weighted_average(pairs: Iterable[tuple[float | None, float | None]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for price, quantity in pairs:
        if price is None or quantity is None:
            continue
        numerator += float(price) * float(quantity)
        denominator += float(quantity)
    if denominator == 0:
        return None
    return numerator / denominator


def _r_multiple(
    *,
    direction: str,
    actual_entry: float,
    actual_exit: float,
    stop_loss: float | None,
) -> float | None:
    """Price-based R-multiple; ``None`` when stop is missing or risk is non-positive."""

    if stop_loss is None:
        return None
    if direction == Direction.SHORT.value:
        risk = stop_loss - actual_entry
        reward = actual_entry - actual_exit
    else:
        risk = actual_entry - stop_loss
        reward = actual_exit - actual_entry
    if risk <= 0:
        return None
    return reward / risk


@dataclass(frozen=True)
class OutcomeRecord:
    """A single fully-closed trade reduced to the fields expectancy stats need."""

    signal_id: str
    symbol: str
    strategy_id: str | None
    sector: str | None
    regime: str | None
    direction: str
    actual_entry: float
    actual_exit: float
    entry_at: datetime
    exit_at: datetime
    pnl: float
    r_multiple: float | None
    time_in_trade_seconds: float
    time_of_day_bucket: str = UNKNOWN_BUCKET
    relative_volume_bucket: str = UNKNOWN_BUCKET
    catalyst_type: str = NO_CATALYST_BUCKET
    spread_bucket: str = UNKNOWN_BUCKET
    volatility_bucket: str = UNKNOWN_BUCKET

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0

    @property
    def is_loser(self) -> bool:
        return self.pnl < 0

    @property
    def exited_before_1030_et(self) -> bool:
        local_exit = self.exit_at.astimezone(_EASTERN)
        return local_exit.timetz().replace(tzinfo=None) < _EARLY_FAILURE_CUTOFF


@dataclass(frozen=True)
class ExpectancyStats:
    """Outcome statistics for a set of closed trades.

    Every metric field is ``None`` when ``sample_size`` is ``0`` so the absence of data is
    explicit rather than rendered as a misleading zero.
    """

    sample_size: int
    r_sample_size: int
    win_rate: float | None
    avg_r: float | None
    median_r: float | None
    max_drawdown: float | None
    drawdown_basis: str | None
    avg_time_to_target_seconds: float | None
    failure_rate_before_1030: float | None
    expectancy: float | None
    matched_on: str | None = None
    version: str = EXPECTANCY_RULE_VERSION


def empty_stats(matched_on: str | None = None) -> ExpectancyStats:
    return ExpectancyStats(
        sample_size=0,
        r_sample_size=0,
        win_rate=None,
        avg_r=None,
        median_r=None,
        max_drawdown=None,
        drawdown_basis=None,
        avg_time_to_target_seconds=None,
        failure_rate_before_1030=None,
        expectancy=None,
        matched_on=matched_on,
    )


def _max_drawdown(records: Sequence[OutcomeRecord]) -> tuple[float | None, str | None]:
    """Peak-to-trough drawdown on the cumulative-R curve, falling back to the pnl curve.

    Returns ``(drawdown, basis)`` where ``drawdown`` is a non-positive number in the units
    of ``basis`` (``"R"`` or ``"pnl"``).
    """

    r_records = [record for record in records if record.r_multiple is not None]
    if r_records:
        ordered = sorted(r_records, key=lambda record: record.exit_at)
        series: list[float] = [float(record.r_multiple) for record in ordered]  # type: ignore[arg-type]
        basis = "R"
    elif records:
        ordered = sorted(records, key=lambda record: record.exit_at)
        series = [record.pnl for record in ordered]
        basis = "pnl"
    else:
        return None, None

    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in series:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst, basis


def compute_stats(
    records: Sequence[OutcomeRecord],
    *,
    matched_on: str | None = None,
) -> ExpectancyStats:
    if not records:
        return empty_stats(matched_on)

    sample_size = len(records)
    pnls = [record.pnl for record in records]
    winners = [record for record in records if record.is_winner]
    win_rate = len(winners) / sample_size
    expectancy = sum(pnls) / sample_size

    r_values = [float(record.r_multiple) for record in records if record.r_multiple is not None]
    r_sample_size = len(r_values)
    avg_r = (sum(r_values) / r_sample_size) if r_values else None
    median_r = median(r_values) if r_values else None

    win_times = [record.time_in_trade_seconds for record in winners]
    avg_time_to_target = (sum(win_times) / len(win_times)) if win_times else None

    early_failures = sum(
        1 for record in records if record.is_loser and record.exited_before_1030_et
    )
    failure_rate_before_1030 = early_failures / sample_size

    max_drawdown, drawdown_basis = _max_drawdown(records)

    return ExpectancyStats(
        sample_size=sample_size,
        r_sample_size=r_sample_size,
        win_rate=round(win_rate, 4),
        avg_r=round(avg_r, 4) if avg_r is not None else None,
        median_r=round(median_r, 4) if median_r is not None else None,
        max_drawdown=round(max_drawdown, 4) if max_drawdown is not None else None,
        drawdown_basis=drawdown_basis,
        avg_time_to_target_seconds=round(avg_time_to_target, 2)
        if avg_time_to_target is not None
        else None,
        failure_rate_before_1030=round(failure_rate_before_1030, 4),
        expectancy=round(expectancy, 4),
        matched_on=matched_on,
    )


def _group_stats(
    records: Sequence[OutcomeRecord],
    *,
    key: Callable[[OutcomeRecord], str],
) -> dict[str, ExpectancyStats]:
    grouped: dict[str, list[OutcomeRecord]] = defaultdict(list)
    for record in records:
        grouped[key(record)].append(record)
    return {
        bucket: compute_stats(bucket_records, matched_on=bucket)
        for bucket, bucket_records in sorted(grouped.items())
    }


class ExpectancyView:
    """An in-memory view over already-loaded closed trades.

    Built once per request so ``summary`` and per-opportunity ``match`` calls do not re-query
    the database.
    """

    def __init__(self, records: Sequence[OutcomeRecord]) -> None:
        self.records: list[OutcomeRecord] = list(records)

    def summary(self) -> dict[str, object]:
        return {
            "overall": compute_stats(self.records, matched_on="overall"),
            "by_strategy": _group_stats(
                self.records, key=lambda record: record.strategy_id or UNKNOWN_BUCKET
            ),
            "by_symbol": _group_stats(self.records, key=lambda record: record.symbol),
            "by_sector": _group_stats(
                self.records, key=lambda record: record.sector or UNKNOWN_BUCKET
            ),
            "by_regime": _group_stats(
                self.records, key=lambda record: record.regime or UNKNOWN_BUCKET
            ),
            "by_market_regime": _group_stats(
                self.records, key=lambda record: record.regime or UNKNOWN_BUCKET
            ),
            "by_time_of_day": _group_stats(
                self.records, key=lambda record: record.time_of_day_bucket
            ),
            "by_relative_volume_bucket": _group_stats(
                self.records, key=lambda record: record.relative_volume_bucket
            ),
            "by_catalyst_type": _group_stats(
                self.records, key=lambda record: record.catalyst_type or NO_CATALYST_BUCKET
            ),
            "by_spread_bucket": _group_stats(
                self.records, key=lambda record: record.spread_bucket
            ),
            "by_volatility_bucket": _group_stats(
                self.records, key=lambda record: record.volatility_bucket
            ),
        }

    def match(
        self,
        *,
        strategy_id: str | None,
        symbol: str | None,
        regime: str | None = None,
    ) -> ExpectancyStats:
        """Return the most specific non-empty stats for an opportunity.

        Widens the historical cohort until trades are found:
        strategy+symbol+regime -> strategy+symbol -> strategy+regime -> strategy -> overall.
        ``matched_on`` records which cohort produced the numbers.
        """

        normalized_symbol = symbol.upper() if symbol else symbol
        candidates: list[tuple[str, Callable[[OutcomeRecord], bool]]] = []
        if strategy_id is not None and normalized_symbol is not None and regime is not None:
            candidates.append(
                (
                    "strategy+symbol+regime",
                    lambda record: record.strategy_id == strategy_id
                    and record.symbol == normalized_symbol
                    and record.regime == regime,
                )
            )
        if strategy_id is not None and normalized_symbol is not None:
            candidates.append(
                (
                    "strategy+symbol",
                    lambda record: record.strategy_id == strategy_id
                    and record.symbol == normalized_symbol,
                )
            )
        if strategy_id is not None and regime is not None:
            candidates.append(
                (
                    "strategy+regime",
                    lambda record: record.strategy_id == strategy_id
                    and record.regime == regime,
                )
            )
        if strategy_id is not None:
            candidates.append(
                ("strategy", lambda record: record.strategy_id == strategy_id)
            )
        candidates.append(("overall", lambda _record: True))

        for label, predicate in candidates:
            subset = [record for record in self.records if predicate(record)]
            if subset:
                return compute_stats(subset, matched_on=label)
        return empty_stats(matched_on="none")


class ExpectancyService:
    """Loads closed-trade outcome records from real fills and exposes expectancy views."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def load(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> ExpectancyView:
        return ExpectancyView(self._load_records(start=start, end=end))

    def _load_records(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[OutcomeRecord]:
        rows = self.session.execute(
            select(models.Fill, models.Order)
            .join(models.Order, models.Fill.order_id == models.Order.id)
            .where(models.Order.signal_id.is_not(None))
            .order_by(models.Fill.source_timestamp.asc(), models.Fill.created_at.asc())
        ).all()
        if not rows:
            return []

        fills_by_signal: dict[str, list[tuple[models.Fill, models.Order]]] = defaultdict(list)
        for fill, order in rows:
            if order.signal_id is None:
                continue
            fills_by_signal[order.signal_id].append((fill, order))
        if not fills_by_signal:
            return []

        signal_ids = list(fills_by_signal.keys())
        signals = {
            signal.id: signal
            for signal in self.session.scalars(
                select(models.Signal).where(models.Signal.id.in_(signal_ids))
            ).all()
        }
        journals = {
            journal.signal_id: journal
            for journal in self.session.scalars(
                select(models.TradeJournal).where(
                    models.TradeJournal.signal_id.in_(signal_ids)
                )
            ).all()
        }

        symbols: set[str] = set()
        for signal_id, fills in fills_by_signal.items():
            signal = signals.get(signal_id)
            symbol = signal.symbol if signal else fills[0][1].symbol
            symbols.add(symbol.upper())
        universe = {
            entry.symbol.upper(): entry
            for entry in self.session.scalars(
                select(models.SymbolUniverse).where(
                    models.SymbolUniverse.symbol.in_(symbols)
                )
            ).all()
        }
        signal_versions = self._latest_signal_versions(signal_ids)
        scanner_contexts = self._scanner_contexts(
            signal_ids=signal_ids,
            signals=signals,
            signal_versions=signal_versions,
        )

        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        records: list[OutcomeRecord] = []
        for signal_id, fills in fills_by_signal.items():
            record = self._build_record(
                signal_id=signal_id,
                fills=fills,
                signal=signals.get(signal_id),
                journal=journals.get(signal_id),
                universe=universe,
                context=scanner_contexts.get(signal_id, {}),
            )
            if record is None:
                continue
            if start_utc is not None and record.exit_at < start_utc:
                continue
            if end_utc is not None and record.exit_at > end_utc:
                continue
            records.append(record)
        return records

    def _latest_signal_versions(self, signal_ids: Sequence[str]) -> dict[str, models.SignalVersion]:
        versions = self.session.scalars(
            select(models.SignalVersion)
            .where(models.SignalVersion.signal_id.in_(signal_ids))
            .order_by(
                models.SignalVersion.signal_id.asc(),
                desc(models.SignalVersion.source_timestamp),
                desc(models.SignalVersion.created_at),
            )
        ).all()
        latest: dict[str, models.SignalVersion] = {}
        for version in versions:
            if version.signal_id not in latest:
                latest[version.signal_id] = version
        return latest

    def _scanner_contexts(
        self,
        *,
        signal_ids: Sequence[str],
        signals: dict[str, models.Signal],
        signal_versions: dict[str, models.SignalVersion],
    ) -> dict[str, dict[str, object]]:
        contexts: dict[str, dict[str, object]] = {signal_id: {} for signal_id in signal_ids}
        scanner_ids = {
            str(version.payload.get("scanner_result_id"))
            for version in signal_versions.values()
            if isinstance(version.payload, dict) and version.payload.get("scanner_result_id")
        }
        scanners_by_id: dict[str, models.ScannerResult] = {}
        if scanner_ids:
            scanners_by_id = {
                row.id: row
                for row in self.session.scalars(
                    select(models.ScannerResult).where(models.ScannerResult.id.in_(scanner_ids))
                ).all()
            }

        fallback_scanners = self.session.scalars(
            select(models.ScannerResult)
            .where(models.ScannerResult.accepted.is_(True))
            .order_by(
                models.ScannerResult.symbol.asc(),
                models.ScannerResult.strategy_id.asc(),
                desc(models.ScannerResult.source_timestamp),
                desc(models.ScannerResult.created_at),
            )
        ).all()

        catalyst_ids: set[str] = set()
        selected_scanners: dict[str, models.ScannerResult] = {}
        for signal_id in signal_ids:
            signal = signals.get(signal_id)
            version = signal_versions.get(signal_id)
            scanner = None
            version_payload = version.payload if version and isinstance(version.payload, dict) else {}
            scanner_id = version_payload.get("scanner_result_id")
            if scanner_id:
                scanner = scanners_by_id.get(str(scanner_id))
            if scanner is None and signal is not None:
                signal_ts = _as_utc(signal.source_timestamp)
                for candidate in fallback_scanners:
                    candidate_ts = _as_utc(candidate.source_timestamp)
                    if candidate.symbol.upper() != signal.symbol.upper():
                        continue
                    if candidate.strategy_id != signal.strategy_id:
                        continue
                    if signal_ts and candidate_ts and candidate_ts > signal_ts:
                        continue
                    scanner = candidate
                    break
            if scanner is None:
                contexts[signal_id] = {"signal_version_payload": version_payload}
                continue
            selected_scanners[signal_id] = scanner
            payload = scanner.payload if isinstance(scanner.payload, dict) else {}
            catalyst_id = _first_str(
                payload,
                (("catalyst_id",), ("request", "catalyst_id"), ("catalyst_reference",)),
            ) or _first_str(version_payload, (("catalyst_reference",),))
            if catalyst_id:
                catalyst_ids.add(catalyst_id)

        catalysts = {
            row.id: row
            for row in self.session.scalars(
                select(models.Catalyst).where(models.Catalyst.id.in_(catalyst_ids))
            ).all()
        } if catalyst_ids else {}
        selected_symbols = {signal.symbol.upper() for signal in signals.values()}
        daily_features = self.session.scalars(
            select(models.FeatureDaily)
            .where(models.FeatureDaily.symbol.in_(selected_symbols))
            .order_by(
                models.FeatureDaily.symbol.asc(),
                desc(models.FeatureDaily.source_timestamp),
                desc(models.FeatureDaily.created_at),
            )
        ).all() if selected_symbols else []
        latest_daily_by_symbol: dict[str, models.FeatureDaily] = {}
        for feature in daily_features:
            latest_daily_by_symbol.setdefault(feature.symbol.upper(), feature)

        for signal_id, scanner in selected_scanners.items():
            payload = scanner.payload if isinstance(scanner.payload, dict) else {}
            version = signal_versions.get(signal_id)
            version_payload = version.payload if version and isinstance(version.payload, dict) else {}
            catalyst_id = _first_str(
                payload,
                (("catalyst_id",), ("request", "catalyst_id"), ("catalyst_reference",)),
            ) or _first_str(version_payload, (("catalyst_reference",),))
            catalyst = catalysts.get(catalyst_id or "")
            daily_feature = latest_daily_by_symbol.get(scanner.symbol.upper())
            contexts[signal_id] = {
                "scanner_payload": payload,
                "signal_version_payload": version_payload,
                "relative_volume": _first_float(
                    payload,
                    (("relative_volume",), ("snapshot", "relative_volume"), ("request", "relative_volume")),
                ),
                "spread_bps": _first_float(
                    payload,
                    (("spread_bps",), ("snapshot", "spread_bps"), ("request", "spread_bps")),
                ),
                "volatility_score": _first_float(
                    payload,
                    (("volatility_score",), ("snapshot", "volatility_score"), ("request", "volatility_score")),
                ) or (daily_feature.volatility_score if daily_feature else None),
                "market_regime": _first_str(
                    payload,
                    (
                        ("market_regime",),
                        ("snapshot", "market_regime"),
                        ("request", "market_regime"),
                        ("preflight", "regime", "market_regime"),
                    ),
                ) or _first_str(version_payload, (("regime_reference",),)),
                "catalyst_type": catalyst.catalyst_type if catalyst else _first_str(
                    payload, (("catalyst_type",), ("request", "catalyst_type"))
                ),
            }
        return contexts

    @staticmethod
    def _build_record(
        *,
        signal_id: str,
        fills: list[tuple[models.Fill, models.Order]],
        signal: models.Signal | None,
        journal: models.TradeJournal | None,
        universe: dict[str, models.SymbolUniverse],
        context: dict[str, object],
    ) -> OutcomeRecord | None:
        first_order = fills[0][1]
        symbol = (signal.symbol if signal else first_order.symbol).upper()
        direction = signal.direction if signal else Direction.LONG.value
        try:
            entry_side = entry_side_from_direction(direction)
        except ValueError:
            entry_side = first_order.side.lower()
        exit_side = "sell" if entry_side == "buy" else "buy"

        entry_fills = [(fill, order) for fill, order in fills if order.side.lower() == entry_side]
        exit_fills = [(fill, order) for fill, order in fills if order.side.lower() == exit_side]
        if not entry_fills or not exit_fills:
            return None

        entry_quantity = sum(float(fill.quantity or 0.0) for fill, _order in entry_fills)
        exit_quantity = sum(float(fill.quantity or 0.0) for fill, _order in exit_fills)
        if entry_quantity <= 0 or exit_quantity < entry_quantity:
            # Not fully closed -- exclude open and partially-exited trades.
            return None

        actual_entry = _weighted_average(
            (fill.price, fill.quantity) for fill, _order in entry_fills
        )
        actual_exit = _weighted_average(
            (fill.price, fill.quantity) for fill, _order in exit_fills
        )
        if actual_entry is None or actual_exit is None:
            return None

        entry_times = [
            ts for ts in (_as_utc(fill.source_timestamp) for fill, _order in entry_fills) if ts is not None
        ]
        exit_times = [
            ts for ts in (_as_utc(fill.source_timestamp) for fill, _order in exit_fills) if ts is not None
        ]
        if not entry_times or not exit_times:
            return None
        entry_at = min(entry_times)
        exit_at = max(exit_times)

        exited_quantity = min(exit_quantity, entry_quantity)
        signed_multiplier = -1.0 if direction == Direction.SHORT.value else 1.0
        commissions = sum(
            float(fill.commission or 0.0) for fill, _order in entry_fills + exit_fills
        )
        pnl = (actual_exit - actual_entry) * exited_quantity * signed_multiplier - commissions

        r_multiple = _r_multiple(
            direction=direction,
            actual_entry=actual_entry,
            actual_exit=actual_exit,
            stop_loss=signal.stop_loss if signal else None,
        )
        time_in_trade_seconds = max(0.0, (exit_at - entry_at).total_seconds())
        sector_row = universe.get(symbol)
        relative_volume = context.get("relative_volume")
        spread_bps = context.get("spread_bps")
        market_regime = journal.market_regime if journal else None
        if not market_regime:
            market_regime = context.get("market_regime") if isinstance(context.get("market_regime"), str) else None
        volatility_score = context.get("volatility_score")
        catalyst_type = context.get("catalyst_type")
        if not catalyst_type and journal and journal.catalyst:
            catalyst_type = journal.catalyst

        return OutcomeRecord(
            signal_id=signal_id,
            symbol=symbol,
            strategy_id=signal.strategy_id if signal else None,
            sector=sector_row.sector if sector_row else None,
            regime=market_regime,
            direction=direction,
            actual_entry=actual_entry,
            actual_exit=actual_exit,
            entry_at=entry_at,
            exit_at=exit_at,
            pnl=pnl,
            r_multiple=r_multiple,
            time_in_trade_seconds=time_in_trade_seconds,
            time_of_day_bucket=time_of_day_bucket(entry_at),
            relative_volume_bucket=relative_volume_bucket(
                float(relative_volume) if isinstance(relative_volume, int | float) else None
            ),
            catalyst_type=str(catalyst_type) if catalyst_type else NO_CATALYST_BUCKET,
            spread_bucket=spread_bucket(
                float(spread_bps) if isinstance(spread_bps, int | float) else None
            ),
            volatility_bucket=volatility_bucket(
                float(volatility_score) if isinstance(volatility_score, int | float) else None
            ),
        )


def latest_market_regime(repository: TradingRepository) -> str | None:
    """Return the most recent recorded market regime, or ``None`` when none exists."""

    snapshot = repository.session.scalar(
        select(models.MarketRegimeSnapshot).order_by(
            desc(models.MarketRegimeSnapshot.source_timestamp),
            desc(models.MarketRegimeSnapshot.created_at),
        )
    )
    return snapshot.market_regime if snapshot else None


def stats_to_dict(stats: ExpectancyStats) -> dict[str, object]:
    return {
        "sample_size": stats.sample_size,
        "r_sample_size": stats.r_sample_size,
        "win_rate": stats.win_rate,
        "avg_r": stats.avg_r,
        "median_r": stats.median_r,
        "max_drawdown": stats.max_drawdown,
        "drawdown_basis": stats.drawdown_basis,
        "avg_time_to_target_seconds": stats.avg_time_to_target_seconds,
        "failure_rate_before_1030": stats.failure_rate_before_1030,
        "expectancy": stats.expectancy,
        "matched_on": stats.matched_on,
        "version": stats.version,
    }
