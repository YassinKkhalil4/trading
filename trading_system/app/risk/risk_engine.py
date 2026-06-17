from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, EnvironmentMode
from trading_system.app.signals.signal_engine import TradeSignal


RISK_RULE_VERSION = "risk_rules_v2"
TARGET_DAILY_RISK_PERCENT = 0.005
TRADING_DAYS_PER_YEAR = 252
EWMA_TRUE_RANGE_PERIODS = 14


@dataclass(frozen=True)
class PortfolioState:
    account_equity: float
    open_positions: int
    daily_loss_pct: float
    weekly_loss_pct: float
    sector_exposure_pct: float
    trades_today: int
    trades_by_strategy_today: dict[str, int]
    symbol_exposure_pct: float = 0.0
    strategy_exposure_pct: float = 0.0
    correlated_exposure_pct: float = 0.0
    overnight_exposure_pct: float = 0.0
    event_risk_active: bool = False
    spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    volatility_score: float | None = None
    kill_switch_active: bool = False
    broker_sync_ok: bool = True
    broker_sync_reason: str = "Broker/internal reconciliation is clean."
    opportunity_score: float | None = None
    opportunity_grade: str | None = None
    expectancy_r: float | None = None
    expectancy_sample_size: int = 0
    recent_strategy_drawdown: float = 0.0
    market_regime: str | None = None
    annualized_volatility: float | None = None
    half_kelly_weight: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    risk_rule_version: str
    position_size: int = 0
    risk_amount: float = 0.0
    risk_multiplier: float = 0.0
    position_size_dollars: float = 0.0
    annualized_volatility: float = 0.0
    half_kelly_weight: float = 0.0


def calculate_ewma_true_range(
    candles: list[dict[str, float]],
    periods: int = EWMA_TRUE_RANGE_PERIODS,
) -> float:
    """Return the EWMA true range for the most recent candles.

    Candles must be ordered oldest to newest and expose high, low, and close values.
    """
    if not candles:
        return 0.0
    alpha = 2 / (periods + 1)
    ewma: float | None = None
    previous_close: float | None = None
    for candle in candles[-periods:]:
        high = float(candle["high"])
        low = float(candle["low"])
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        ewma = true_range if ewma is None else (alpha * true_range) + ((1 - alpha) * ewma)
        previous_close = float(candle["close"])
    return float(ewma or 0.0)


def calculate_annualized_volatility_from_ewma_true_range(ewma_true_range: float, current_price: float) -> float:
    if ewma_true_range <= 0 or current_price <= 0:
        return 0.0
    return (ewma_true_range / current_price) * sqrt(TRADING_DAYS_PER_YEAR)


def calculate_volatility_targeted_position_size_dollars(
    *,
    portfolio_value: float,
    current_annualized_volatility: float,
    target_daily_risk_percent: float = TARGET_DAILY_RISK_PERCENT,
) -> float:
    if portfolio_value <= 0 or current_annualized_volatility <= 0 or target_daily_risk_percent <= 0:
        return 0.0
    return (
        (portfolio_value * target_daily_risk_percent)
        / (current_annualized_volatility * sqrt(1 / TRADING_DAYS_PER_YEAR))
    )


def calculate_half_kelly_weight(*, win_rate: float, win_loss_ratio: float) -> float:
    if win_loss_ratio <= 0:
        return 0.0
    kelly_percentage = win_rate - ((1 - win_rate) / win_loss_ratio)
    return kelly_percentage / 2


class RiskEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        decision_logger: InMemoryDecisionLogger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.decision_logger = decision_logger or InMemoryDecisionLogger()

    def evaluate(self, signal: TradeSignal, portfolio: PortfolioState) -> RiskDecision:
        if self.settings.environment_mode == EnvironmentMode.LIVE and not (
            self.settings.allow_live_trading
            and self.settings.confirm_live_trading == "I_UNDERSTAND_RISK"
            and self.settings.live_order_path_enabled
        ):
            return self._reject(signal, "Live risk path requires explicit live configuration gates.")
        if portfolio.kill_switch_active:
            return self._reject(signal, "Kill switch is active.")
        if not portfolio.broker_sync_ok:
            return self._reject(signal, f"Broker/internal reconciliation failed: {portfolio.broker_sync_reason}")
        if portfolio.daily_loss_pct >= self.settings.max_daily_loss_pct:
            return self._reject(signal, "Max daily loss reached.")
        if portfolio.weekly_loss_pct >= self.settings.max_weekly_loss_pct:
            return self._reject(signal, "Max weekly loss reached.")
        if portfolio.open_positions >= self.settings.max_open_positions:
            return self._reject(signal, "Max open positions reached.")
        if portfolio.sector_exposure_pct >= self.settings.max_single_sector_exposure_pct:
            return self._reject(signal, "Max single-sector exposure reached.")
        if portfolio.symbol_exposure_pct >= self.settings.max_symbol_exposure_pct:
            return self._reject(signal, "Max symbol exposure reached.")
        if portfolio.strategy_exposure_pct >= self.settings.max_strategy_exposure_pct:
            return self._reject(signal, "Max strategy exposure reached.")
        if portfolio.correlated_exposure_pct >= self.settings.max_correlated_exposure_pct:
            return self._reject(signal, "Max correlated exposure reached.")
        if portfolio.overnight_exposure_pct >= self.settings.max_overnight_exposure_pct:
            return self._reject(signal, "Max overnight exposure reached.")
        if portfolio.event_risk_active:
            return self._reject(signal, "Event risk block is active.")
        if portfolio.spread_bps > self.settings.max_spread_bps:
            return self._reject(signal, "Spread exceeds configured limit.")
        if portfolio.expected_slippage_bps > self.settings.max_slippage_bps:
            return self._reject(signal, "Expected slippage exceeds configured limit.")
        if (
            portfolio.volatility_score is not None
            and portfolio.volatility_score >= self.settings.max_volatility_score
        ):
            return self._reject(signal, "Volatility score exceeds configured limit.")
        if portfolio.trades_today >= self.settings.max_trades_per_day:
            return self._reject(signal, "Max trades per day reached.")
        strategy_count = portfolio.trades_by_strategy_today.get(signal.strategy_id, 0)
        if strategy_count >= self.settings.max_trades_per_strategy_per_day:
            return self._reject(signal, "Max trades per strategy per day reached.")

        entry = signal.entry_zone[0]
        risk_per_share = entry - signal.stop_loss
        if risk_per_share <= 0:
            return self._reject(signal, "Invalid stop loss: risk per share must be positive.")

        if portfolio.expectancy_r is not None and portfolio.expectancy_r < 0:
            return self._reject(signal, "Strategy expectancy is negative.")
        if portfolio.half_kelly_weight is not None and portfolio.half_kelly_weight <= 0:
            return self._reject(signal, "Half-Kelly allocation is non-positive.")

        annualized_volatility = portfolio.annualized_volatility
        if annualized_volatility is None or annualized_volatility <= 0:
            return self._reject(signal, "Annualized volatility is required for volatility-targeted sizing.")

        position_size_dollars = calculate_volatility_targeted_position_size_dollars(
            portfolio_value=portfolio.account_equity,
            current_annualized_volatility=annualized_volatility,
        )
        if portfolio.half_kelly_weight is not None:
            position_size_dollars *= portfolio.half_kelly_weight
        position_size = int(position_size_dollars // entry)
        if position_size <= 0:
            return self._reject(signal, "Volatility-targeted allocation is too small for this entry price.")

        decision = RiskDecision(
            approved=True,
            reason="Risk checks approved with volatility-targeted sizing.",
            risk_rule_version=RISK_RULE_VERSION,
            position_size=position_size,
            risk_amount=position_size_dollars,
            risk_multiplier=portfolio.half_kelly_weight if portfolio.half_kelly_weight is not None else 1.0,
            position_size_dollars=position_size_dollars,
            annualized_volatility=annualized_volatility,
            half_kelly_weight=portfolio.half_kelly_weight if portfolio.half_kelly_weight is not None else 1.0,
        )
        self.decision_logger.record_simple(
            DecisionType.RISK,
            DecisionOutcome.APPROVED,
            decision.reason,
            entity_id=signal.idempotency_key,
            strategy_id=signal.strategy_id,
            rule_version=RISK_RULE_VERSION,
        )
        return decision

    def _reject(self, signal: TradeSignal, reason: str) -> RiskDecision:
        self.decision_logger.record_simple(
            DecisionType.RISK,
            DecisionOutcome.REJECTED,
            reason,
            entity_id=signal.idempotency_key,
            strategy_id=signal.strategy_id,
            rule_version=RISK_RULE_VERSION,
        )
        return RiskDecision(False, reason, RISK_RULE_VERSION)
