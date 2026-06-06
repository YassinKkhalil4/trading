from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.core.enums import MarketRegime


REGIME_RULE_VERSION = "market_regime_v1"


@dataclass(frozen=True)
class RegimeInputs:
    spy_above_20ma: bool
    spy_above_50ma: bool
    qqq_above_20ma: bool
    vix_level: float
    breadth_positive: bool


@dataclass(frozen=True)
class RegimeDecision:
    market_regime: MarketRegime
    confidence: float
    allowed_bias: str
    risk_multiplier: float
    breakout_permission: bool
    mean_reversion_permission: str
    reason: str
    rule_version: str = REGIME_RULE_VERSION


def classify_market_regime(inputs: RegimeInputs) -> RegimeDecision:
    if (
        inputs.spy_above_20ma
        and inputs.spy_above_50ma
        and inputs.qqq_above_20ma
        and inputs.vix_level < 20
        and inputs.breadth_positive
    ):
        return RegimeDecision(
            market_regime=MarketRegime.BULL_TREND,
            confidence=82,
            allowed_bias="LONG_PREFERRED",
            risk_multiplier=1.0,
            breakout_permission=True,
            mean_reversion_permission="limited",
            reason="Indexes are above key moving averages, VIX is below 20, and breadth is positive.",
        )
    if inputs.vix_level >= 30:
        return RegimeDecision(
            market_regime=MarketRegime.HIGH_VOLATILITY,
            confidence=75,
            allowed_bias="REDUCED_SIZE",
            risk_multiplier=0.5,
            breakout_permission=False,
            mean_reversion_permission="limited",
            reason="VIX is elevated.",
        )
    if not inputs.spy_above_50ma and not inputs.qqq_above_20ma:
        return RegimeDecision(
            market_regime=MarketRegime.BEAR_TREND,
            confidence=75,
            allowed_bias="SHORT_OR_CASH",
            risk_multiplier=0.5,
            breakout_permission=False,
            mean_reversion_permission="limited",
            reason="Major indexes are below key moving averages.",
        )
    return RegimeDecision(
        market_regime=MarketRegime.CHOPPY,
        confidence=60,
        allowed_bias="SELECTIVE",
        risk_multiplier=0.75,
        breakout_permission=False,
        mean_reversion_permission="limited",
        reason="Mixed trend, volatility, or breadth inputs.",
    )

