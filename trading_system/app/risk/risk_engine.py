from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, EnvironmentMode
from trading_system.app.signals.signal_engine import TradeSignal


RISK_RULE_VERSION = "risk_rules_v1"


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


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    risk_rule_version: str
    position_size: int = 0
    risk_amount: float = 0.0
    risk_multiplier: float = 0.0


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

        risk_multiplier = self._adaptive_risk_multiplier(portfolio)
        if risk_multiplier <= 0:
            return self._reject(signal, "Opportunity score/expectancy does not allow risk allocation.")
        risk_amount = portfolio.account_equity * (self.settings.risk_per_trade_pct / 100) * risk_multiplier
        position_size = int(risk_amount // risk_per_share)
        if position_size <= 0:
            return self._reject(signal, "Account risk amount is too small for this stop distance.")

        decision = RiskDecision(
            approved=True,
            reason="Risk checks approved.",
            risk_rule_version=RISK_RULE_VERSION,
            position_size=position_size,
            risk_amount=risk_amount,
            risk_multiplier=risk_multiplier,
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

    def _adaptive_risk_multiplier(self, portfolio: PortfolioState) -> float:
        if portfolio.expectancy_r is not None and portfolio.expectancy_r < 0:
            return 0.0
        grade = (portfolio.opportunity_grade or "").upper()
        if grade == "A+":
            multiplier = 1.0
        elif grade == "A":
            multiplier = 0.75
        elif grade == "B":
            multiplier = 0.5
        elif grade == "C":
            multiplier = 0.1 if self.settings.environment_mode == EnvironmentMode.PAPER else 0.0
        elif portfolio.opportunity_score is not None:
            if portfolio.opportunity_score >= 90:
                multiplier = 1.0
            elif portfolio.opportunity_score >= 80:
                multiplier = 0.75
            elif portfolio.opportunity_score >= 70:
                multiplier = 0.5
            elif portfolio.opportunity_score >= 60 and self.settings.environment_mode == EnvironmentMode.PAPER:
                multiplier = 0.1
            else:
                multiplier = 0.0
        else:
            multiplier = 1.0

        has_alpha_context = portfolio.opportunity_grade is not None or portfolio.opportunity_score is not None
        if has_alpha_context and (portfolio.expectancy_r is None or portfolio.expectancy_sample_size < 20):
            multiplier *= 0.5
        if portfolio.recent_strategy_drawdown < -2.0:
            multiplier *= 0.5
        if portfolio.market_regime in {"BEAR_TREND", "RISK_OFF", "HIGH_VOLATILITY", "MACRO_EVENT_RISK"}:
            multiplier *= 0.5
        if portfolio.volatility_score is not None and portfolio.volatility_score > 75:
            multiplier *= 0.75
        return round(max(0.0, min(1.0, multiplier)), 4)

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
