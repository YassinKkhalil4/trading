from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, TradeType
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.signals.signal_engine import TradeSignal

PORTFOLIO_RULE_VERSION = "portfolio_engine_v1"
UNKNOWN_SECTOR = "UNKNOWN"


class PortfolioDecisionOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REDUCED_SIZE = "REDUCED_SIZE"


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    quantity: float
    average_price: float
    sector: str | None = None
    strategy_id: str | None = None
    is_overnight: bool = False


@dataclass(frozen=True)
class PortfolioOpenOrder:
    symbol: str
    quantity: float
    limit_price: float
    side: str = "buy"
    sector: str | None = None
    strategy_id: str | None = None


@dataclass(frozen=True)
class PortfolioEvaluationContext:
    account_equity: float
    positions: tuple[PortfolioPosition, ...] = ()
    open_orders: tuple[PortfolioOpenOrder, ...] = ()
    account_cash: float | None = None
    symbol_sectors: dict[str, str] | None = None
    correlated_groups: dict[str, frozenset[str]] | None = None
    pending_same_sector_signal_count: int = 0
    approved_same_sector_signal_count: int = 0


@dataclass(frozen=True)
class ExposureSnapshot:
    account_equity: float
    account_cash: float | None
    open_positions: int
    sector_exposure_pct: dict[str, float]
    symbol_exposure_pct: dict[str, float]
    strategy_exposure_pct: dict[str, float]
    correlated_exposure_pct: float
    overnight_exposure_pct: float
    signal_sector: str
    signal_symbol_exposure_pct: float
    signal_sector_exposure_pct: float
    signal_strategy_exposure_pct: float
    proposed_exposure_pct: float
    projected_cash_pct: float | None


@dataclass(frozen=True)
class PortfolioDecision:
    outcome: PortfolioDecisionOutcome
    recommended_size_multiplier: float
    reasons: tuple[str, ...]
    exposure_snapshot: ExposureSnapshot
    signal_id: str
    portfolio_rule_version: str = PORTFOLIO_RULE_VERSION

    @property
    def approved(self) -> bool:
        return self.outcome in {PortfolioDecisionOutcome.APPROVED, PortfolioDecisionOutcome.REDUCED_SIZE}


# Required schema if a dedicated portfolio_decisions table is added later:
# - signal_id (FK signals.id)
# - outcome (APPROVED|REJECTED|REDUCED_SIZE)
# - recommended_size_multiplier (float)
# - reasons (JSON array)
# - exposure_snapshot (JSON)
# - portfolio_rule_version (string)
# - source_timestamp (datetime)
# Until then, decisions persist through decision_logs with entity_type="portfolio_decision".


class PortfolioDecisionService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: TradingRepository | None = None,
        snapshot_service: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self.snapshot_service = snapshot_service

    def evaluate(
        self,
        *,
        signal_id: str,
        signal: TradeSignal,
        context: PortfolioEvaluationContext,
        persist: bool = True,
    ) -> PortfolioDecision:
        if context.account_equity <= 0:
            decision = self._reject(
                signal_id=signal_id,
                signal=signal,
                context=context,
                reasons=("Account equity must be positive.",),
                exposure_snapshot=_empty_snapshot(context, signal, self.settings),
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        exposure_snapshot = build_exposure_snapshot(signal=signal, context=context, settings=self.settings)
        reasons: list[str] = []
        hard_multiplier = 1.0

        if not _valid_signal_risk_geometry(signal):
            decision = self._reject(
                signal_id=signal_id,
                signal=signal,
                context=context,
                reasons=("Invalid stop loss: risk per share must be positive.",),
                exposure_snapshot=exposure_snapshot,
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        if exposure_snapshot.proposed_exposure_pct <= 0:
            decision = self._reject(
                signal_id=signal_id,
                signal=signal,
                context=context,
                reasons=("Proposed exposure is too small for portfolio limits.",),
                exposure_snapshot=exposure_snapshot,
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        if _creates_new_position(signal.symbol, context):
            if exposure_snapshot.open_positions >= self.settings.max_open_positions:
                reasons.append("Max open positions reached.")

        if context.pending_same_sector_signal_count >= self.settings.max_same_sector_new_signals:
            reasons.append(
                f"Max same-sector new signals reached ({self.settings.max_same_sector_new_signals})."
            )

        if reasons:
            decision = self._reject(
                signal_id=signal_id,
                signal=signal,
                context=context,
                reasons=tuple(reasons),
                exposure_snapshot=exposure_snapshot,
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        soft_multiplier = _soft_limit_multiplier(
            signal=signal,
            context=context,
            settings=self.settings,
            exposure_snapshot=exposure_snapshot,
            reasons=reasons,
        )
        recommended_multiplier = min(hard_multiplier, soft_multiplier)

        if recommended_multiplier <= 0:
            decision = self._reject(
                signal_id=signal_id,
                signal=signal,
                context=context,
                reasons=tuple(reasons) or ("Portfolio limits block this signal.",),
                exposure_snapshot=exposure_snapshot,
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        if recommended_multiplier < 1.0:
            reasons.append(
                f"Size reduced to {recommended_multiplier:.2f}x to stay within portfolio limits."
            )
            decision = PortfolioDecision(
                outcome=PortfolioDecisionOutcome.REDUCED_SIZE,
                recommended_size_multiplier=round(recommended_multiplier, 4),
                reasons=tuple(reasons),
                exposure_snapshot=exposure_snapshot,
                signal_id=signal_id,
            )
            self._maybe_persist(decision, signal, persist)
            return decision

        decision = PortfolioDecision(
            outcome=PortfolioDecisionOutcome.APPROVED,
            recommended_size_multiplier=1.0,
            reasons=("Portfolio checks approved.",),
            exposure_snapshot=exposure_snapshot,
            signal_id=signal_id,
        )
        self._maybe_persist(decision, signal, persist)
        return decision

    def _reject(
        self,
        *,
        signal_id: str,
        signal: TradeSignal,
        context: PortfolioEvaluationContext,
        reasons: tuple[str, ...],
        exposure_snapshot: ExposureSnapshot,
    ) -> PortfolioDecision:
        return PortfolioDecision(
            outcome=PortfolioDecisionOutcome.REJECTED,
            recommended_size_multiplier=0.0,
            reasons=reasons,
            exposure_snapshot=exposure_snapshot,
            signal_id=signal_id,
        )

    def _maybe_persist(self, decision: PortfolioDecision, signal: TradeSignal, persist: bool) -> None:
        if not persist or self.repository is None:
            return
        outcome = (
            DecisionOutcome.REJECTED
            if decision.outcome == PortfolioDecisionOutcome.REJECTED
            else DecisionOutcome.APPROVED
        )
        self.repository.store_decision_log(
            decision_type=DecisionType.STRATEGY,
            outcome=outcome,
            entity_type="portfolio_decision",
            entity_id=decision.signal_id,
            strategy_id=signal.strategy_id,
            rule_version=decision.portfolio_rule_version,
            reason="; ".join(decision.reasons),
            payload={
                "portfolio_outcome": decision.outcome.value,
                "recommended_size_multiplier": decision.recommended_size_multiplier,
                "reasons": list(decision.reasons),
                "exposure_snapshot": _exposure_snapshot_payload(decision.exposure_snapshot),
            },
        )
        from trading_system.app.services.replay.decision_snapshot_service import DecisionSnapshotService

        snapshot_service = self.snapshot_service or DecisionSnapshotService(self.repository)
        snapshot_service.capture_portfolio_decision(
            signal,
            decision,
            source_timestamp=signal.source_timestamp,
        )


def build_exposure_snapshot(
    *,
    signal: TradeSignal,
    context: PortfolioEvaluationContext,
    settings: Settings,
) -> ExposureSnapshot:
    equity = context.account_equity
    symbol_sectors = context.symbol_sectors or {}
    sector_exposure: dict[str, float] = {}
    symbol_exposure: dict[str, float] = {}
    strategy_exposure: dict[str, float] = {}
    correlated_notionals: dict[str, float] = {}
    overnight_notional = 0.0

    for position in context.positions:
        if position.quantity == 0:
            continue
        notional = abs(position.quantity) * max(position.average_price, 0.0)
        sector = _resolve_sector(position.symbol, position.sector, symbol_sectors)
        strategy_id = position.strategy_id or "UNKNOWN"
        _add_exposure(sector_exposure, sector, notional, equity)
        _add_exposure(symbol_exposure, position.symbol.upper(), notional, equity)
        _add_exposure(strategy_exposure, strategy_id, notional, equity)
        _add_correlated_notional(
            correlated_notionals,
            position.symbol,
            sector,
            context.correlated_groups,
            notional,
        )
        if position.is_overnight:
            overnight_notional += notional

    for order in context.open_orders:
        notional = abs(order.quantity) * max(order.limit_price, 0.0)
        sector = _resolve_sector(order.symbol, order.sector, symbol_sectors)
        strategy_id = order.strategy_id or "UNKNOWN"
        _add_exposure(sector_exposure, sector, notional, equity)
        _add_exposure(symbol_exposure, order.symbol.upper(), notional, equity)
        _add_exposure(strategy_exposure, strategy_id, notional, equity)
        _add_correlated_notional(
            correlated_notionals,
            order.symbol,
            sector,
            context.correlated_groups,
            notional,
        )

    signal_sector = _resolve_sector(signal.symbol, None, symbol_sectors)
    proposed_exposure_pct = estimate_proposed_exposure_pct(signal, equity, settings)
    signal_symbol_exposure = symbol_exposure.get(signal.symbol.upper(), 0.0) + proposed_exposure_pct
    signal_sector_exposure = sector_exposure.get(signal_sector, 0.0) + proposed_exposure_pct
    signal_strategy_exposure = strategy_exposure.get(signal.strategy_id, 0.0) + proposed_exposure_pct
    correlated_group = _correlation_group_key(signal.symbol, signal_sector, context.correlated_groups)
    correlated_exposure_pct = (
        (correlated_notionals.get(correlated_group, 0.0) / equity) * 100.0
    ) + proposed_exposure_pct

    if signal.trade_type != TradeType.DAY_TRADE:
        overnight_notional += (proposed_exposure_pct / 100.0) * equity

    projected_cash_pct = None
    if context.account_cash is not None and equity > 0:
        proposed_notional = (proposed_exposure_pct / 100.0) * equity
        projected_cash_pct = ((context.account_cash - proposed_notional) / equity) * 100.0

    return ExposureSnapshot(
        account_equity=equity,
        account_cash=context.account_cash,
        open_positions=_count_open_positions(context),
        sector_exposure_pct={key: round(value, 4) for key, value in sector_exposure.items()},
        symbol_exposure_pct={key: round(value, 4) for key, value in symbol_exposure.items()},
        strategy_exposure_pct={key: round(value, 4) for key, value in strategy_exposure.items()},
        correlated_exposure_pct=round(correlated_exposure_pct, 4),
        overnight_exposure_pct=round((overnight_notional / equity) * 100.0, 4) if equity else 0.0,
        signal_sector=signal_sector,
        signal_symbol_exposure_pct=round(signal_symbol_exposure, 4),
        signal_strategy_exposure_pct=round(signal_strategy_exposure, 4),
        signal_sector_exposure_pct=round(signal_sector_exposure, 4),
        proposed_exposure_pct=round(proposed_exposure_pct, 4),
        projected_cash_pct=round(projected_cash_pct, 4) if projected_cash_pct is not None else None,
    )


def estimate_proposed_exposure_pct(signal: TradeSignal, account_equity: float, settings: Settings) -> float:
    entry = signal.entry_zone[0]
    risk_per_share = entry - signal.stop_loss
    if risk_per_share <= 0 or account_equity <= 0:
        return 0.0
    risk_amount = account_equity * (settings.risk_per_trade_pct / 100.0)
    position_size = int(risk_amount // risk_per_share)
    if position_size <= 0:
        return 0.0
    notional = position_size * entry
    return (notional / account_equity) * 100.0


def _soft_limit_multiplier(
    *,
    signal: TradeSignal,
    context: PortfolioEvaluationContext,
    settings: Settings,
    exposure_snapshot: ExposureSnapshot,
    reasons: list[str],
) -> float:
    multipliers = [1.0]
    proposed = exposure_snapshot.proposed_exposure_pct
    if proposed <= 0:
        return 0.0

    current_symbol = exposure_snapshot.signal_symbol_exposure_pct - proposed
    current_sector = exposure_snapshot.signal_sector_exposure_pct - proposed
    current_strategy = exposure_snapshot.signal_strategy_exposure_pct - proposed
    current_correlated = exposure_snapshot.correlated_exposure_pct - proposed
    current_overnight = exposure_snapshot.overnight_exposure_pct
    if signal.trade_type != TradeType.DAY_TRADE:
        current_overnight -= proposed

    multipliers.append(
        _exposure_multiplier(
            current=current_symbol,
            proposed=proposed,
            limit=settings.max_symbol_exposure_pct,
            label="symbol",
            reasons=reasons,
        )
    )
    multipliers.append(
        _exposure_multiplier(
            current=current_sector,
            proposed=proposed,
            limit=settings.max_single_sector_exposure_pct,
            label="sector",
            reasons=reasons,
        )
    )
    multipliers.append(
        _exposure_multiplier(
            current=current_strategy,
            proposed=proposed,
            limit=settings.max_strategy_exposure_pct,
            label="strategy",
            reasons=reasons,
        )
    )
    multipliers.append(
        _exposure_multiplier(
            current=current_correlated,
            proposed=proposed,
            limit=settings.max_correlated_exposure_pct,
            label="correlated",
            reasons=reasons,
        )
    )
    multipliers.append(
        _exposure_multiplier(
            current=current_overnight,
            proposed=proposed,
            limit=settings.max_overnight_exposure_pct,
            label="overnight",
            reasons=reasons,
        )
    )

    if context.account_cash is not None and context.account_equity > 0:
        min_cash_pct = settings.min_cash_buffer_pct
        available_cash_pct = (context.account_cash / context.account_equity) * 100.0
        headroom_pct = available_cash_pct - min_cash_pct
        if headroom_pct <= 0:
            reasons.append("Cash buffer already below configured minimum.")
            return 0.0
        cash_multiplier = headroom_pct / proposed
        if cash_multiplier < 1.0:
            reasons.append(
                f"Cash buffer requires size reduction (min cash {min_cash_pct:.2f}%)."
            )
        multipliers.append(min(1.0, cash_multiplier))

    return min(multipliers)


def _exposure_multiplier(
    *,
    current: float,
    proposed: float,
    limit: float,
    label: str,
    reasons: list[str],
) -> float:
    if proposed <= 0:
        return 1.0
    if current + proposed <= limit:
        return 1.0
    headroom = limit - current
    if headroom <= 0:
        reasons.append(f"Max {label} exposure reached.")
        return 0.0
    multiplier = headroom / proposed
    reasons.append(f"Max {label} exposure requires size reduction.")
    return min(1.0, multiplier)


def _count_open_positions(context: PortfolioEvaluationContext) -> int:
    symbols = {
        position.symbol.upper()
        for position in context.positions
        if position.quantity != 0
    }
    for order in context.open_orders:
        if order.quantity != 0:
            symbols.add(order.symbol.upper())
    return len(symbols)


def _creates_new_position(symbol: str, context: PortfolioEvaluationContext) -> bool:
    symbol = symbol.upper()
    for position in context.positions:
        if position.symbol.upper() == symbol and position.quantity != 0:
            return False
    for order in context.open_orders:
        if order.symbol.upper() == symbol and order.quantity != 0:
            return False
    return True


def _valid_signal_risk_geometry(signal: TradeSignal) -> bool:
    entry = signal.entry_zone[0]
    return entry - signal.stop_loss > 0


def _resolve_sector(symbol: str, explicit: str | None, symbol_sectors: dict[str, str]) -> str:
    if explicit:
        return explicit
    return symbol_sectors.get(symbol.upper(), UNKNOWN_SECTOR)


def _correlation_group_key(
    symbol: str,
    sector: str,
    correlated_groups: dict[str, frozenset[str]] | None,
) -> str:
    symbol = symbol.upper()
    if correlated_groups:
        for group_name, members in correlated_groups.items():
            if symbol in members:
                return group_name
    return sector


def _add_exposure(bucket: dict[str, float], key: str, notional: float, equity: float) -> None:
    if equity <= 0:
        return
    bucket[key] = bucket.get(key, 0.0) + (notional / equity) * 100.0


def _add_correlated_notional(
    bucket: dict[str, float],
    symbol: str,
    sector: str,
    correlated_groups: dict[str, frozenset[str]] | None,
    notional: float,
) -> None:
    group = _correlation_group_key(symbol, sector, correlated_groups)
    bucket[group] = bucket.get(group, 0.0) + notional


def _empty_snapshot(
    context: PortfolioEvaluationContext,
    signal: TradeSignal,
    settings: Settings,
) -> ExposureSnapshot:
    return build_exposure_snapshot(signal=signal, context=context, settings=settings)


def _exposure_snapshot_payload(snapshot: ExposureSnapshot) -> dict[str, Any]:
    return {
        "account_equity": snapshot.account_equity,
        "account_cash": snapshot.account_cash,
        "open_positions": snapshot.open_positions,
        "sector_exposure_pct": snapshot.sector_exposure_pct,
        "symbol_exposure_pct": snapshot.symbol_exposure_pct,
        "strategy_exposure_pct": snapshot.strategy_exposure_pct,
        "correlated_exposure_pct": snapshot.correlated_exposure_pct,
        "overnight_exposure_pct": snapshot.overnight_exposure_pct,
        "signal_sector": snapshot.signal_sector,
        "signal_symbol_exposure_pct": snapshot.signal_symbol_exposure_pct,
        "signal_sector_exposure_pct": snapshot.signal_sector_exposure_pct,
        "signal_strategy_exposure_pct": snapshot.signal_strategy_exposure_pct,
        "proposed_exposure_pct": snapshot.proposed_exposure_pct,
        "projected_cash_pct": snapshot.projected_cash_pct,
    }
