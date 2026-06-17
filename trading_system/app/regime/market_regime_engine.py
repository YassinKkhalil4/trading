from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module

import numpy as np
import pandas as pd

from trading_system.app.core.enums import MarketRegime


REGIME_RULE_VERSION = "market_regime_hmm_v1"
HMM_COMPONENTS = 3
HMM_CHOP_PROBABILITY_THRESHOLD = 0.70
HMM_STATE_LABELS = {
    "bull_trend": MarketRegime.BULL_TREND,
    "bear_trend": MarketRegime.BEAR_TREND,
    "high_variance_chop": MarketRegime.CHOPPY,
}


@dataclass(frozen=True)
class RegimeInputs:
    spy_returns: pd.Series
    spy_variance: pd.Series


@dataclass(frozen=True)
class RegimeDecision:
    market_regime: MarketRegime
    confidence: float
    allowed_bias: str
    risk_multiplier: float
    breakout_permission: bool
    mean_reversion_permission: str
    reason: str
    hmm_state: int
    hmm_state_probabilities: dict[str, float] = field(default_factory=dict)
    restricted: bool = False
    rule_version: str = REGIME_RULE_VERSION


def build_hmm_inputs(spy_frame: pd.DataFrame, *, bars_per_day: int = 78, days: int = 5) -> RegimeInputs:
    """Build the 5-day, 5-minute SPY return/variance emission matrix."""
    close = spy_frame["close"].astype(float)
    returns = close.pct_change()
    window = bars_per_day * days
    variance = returns.rolling(window=window, min_periods=max(20, bars_per_day)).var()
    features = pd.concat([returns.rename("spy_return"), variance.rename("spy_variance")], axis=1).dropna().tail(window)
    return RegimeInputs(spy_returns=features["spy_return"], spy_variance=features["spy_variance"])


def classify_market_regime(inputs: RegimeInputs) -> RegimeDecision:
    features = _feature_matrix(inputs)
    if len(features) < HMM_COMPONENTS * 20:
        return RegimeDecision(
            market_regime=MarketRegime.CHOPPY,
            confidence=50.0,
            allowed_bias="SELECTIVE",
            risk_multiplier=0.75,
            breakout_permission=False,
            mean_reversion_permission="limited",
            reason="Not enough 5-minute SPY return/variance emissions to fit a 3-state Gaussian HMM.",
            hmm_state=-1,
            hmm_state_probabilities={},
        )

    GaussianHMM = import_module("hmmlearn.hmm").GaussianHMM
    model = GaussianHMM(n_components=HMM_COMPONENTS, covariance_type="full", n_iter=200, random_state=42)
    model.fit(features)
    probabilities = model.predict_proba(features)[-1]
    current_state = int(np.argmax(probabilities))
    state_labels = _label_states(model.means_)
    probability_by_label = {label: float(probabilities[state]) for state, label in state_labels.items()}
    chop_probability = probability_by_label.get("high_variance_chop", 0.0)
    if chop_probability > HMM_CHOP_PROBABILITY_THRESHOLD:
        return RegimeDecision(
            market_regime=MarketRegime.CHOPPY,
            confidence=round(chop_probability * 100.0, 2),
            allowed_bias="HALT_NON_HEDGING",
            risk_multiplier=0.0,
            breakout_permission=False,
            mean_reversion_permission="blocked",
            reason=(
                "3-state Gaussian HMM classifies the current 5-day 5-minute SPY emission "
                f"window as high-variance chop with {chop_probability:.1%} probability; "
                "non-hedging market orders are restricted."
            ),
            hmm_state=current_state,
            hmm_state_probabilities=_indexed_probabilities(probabilities, state_labels),
            restricted=True,
        )

    label = state_labels[current_state]
    market_regime = HMM_STATE_LABELS[label]
    confidence = round(float(probabilities[current_state]) * 100.0, 2)
    bullish = label == "bull_trend"
    return RegimeDecision(
        market_regime=market_regime,
        confidence=confidence,
        allowed_bias="LONG_PREFERRED" if bullish else "SHORT_OR_CASH",
        risk_multiplier=1.0 if bullish else 0.5,
        breakout_permission=bullish,
        mean_reversion_permission="limited",
        reason=(
            "3-state Gaussian HMM classified the current 5-day 5-minute SPY return/variance "
            f"window as {label.replace('_', ' ')} with {confidence:.1f}% state probability."
        ),
        hmm_state=current_state,
        hmm_state_probabilities=_indexed_probabilities(probabilities, state_labels),
    )


def _feature_matrix(inputs: RegimeInputs) -> np.ndarray:
    features = pd.concat([inputs.spy_returns.rename("spy_return"), inputs.spy_variance.rename("spy_variance")], axis=1)
    return features.replace([np.inf, -np.inf], np.nan).dropna().to_numpy(dtype=float)


def _label_states(means: np.ndarray) -> dict[int, str]:
    variance_order = np.argsort(means[:, 1])
    labels: dict[int, str] = {int(variance_order[-1]): "high_variance_chop"}
    remaining = [int(state) for state in variance_order[:-1]]
    remaining.sort(key=lambda state: means[state, 0])
    labels[remaining[0]] = "bear_trend"
    labels[remaining[-1]] = "bull_trend"
    return labels


def _indexed_probabilities(probabilities: np.ndarray, labels: dict[int, str]) -> dict[str, float]:
    payload: dict[str, float] = {}
    for state, probability in enumerate(probabilities):
        label = labels.get(state, "unknown")
        value = round(float(probability), 6)
        payload[str(state)] = value
        payload[label] = value
    return payload
