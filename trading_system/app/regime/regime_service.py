from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.db.repositories import TradingRepository
from trading_system.app.regime.market_regime_engine import build_hmm_inputs, classify_market_regime


REGIME_SERVICE_VERSION = "regime_service_v1"


@dataclass(frozen=True)
class RegimeRunResult:
    computed: bool
    market_regime: str | None
    confidence: float | None
    reason: str
    hmm_state_probabilities: dict[str, float] | None = None
    version: str = REGIME_SERVICE_VERSION


class MarketRegimeService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self) -> RegimeRunResult:
        spy = self._frame("SPY")
        if len(spy) < 60:
            return RegimeRunResult(
                False,
                None,
                None,
                "Not enough SPY 5-minute clean candles to compute the HMM market regime.",
                None,
            )
        inputs = build_hmm_inputs(spy)
        decision = classify_market_regime(inputs)
        self.repository.store_market_regime_snapshot(
            market_regime=decision.market_regime.value,
            confidence=decision.confidence,
            allowed_bias=decision.allowed_bias,
            risk_multiplier=decision.risk_multiplier,
            breakout_permission=decision.breakout_permission,
            mean_reversion_permission=decision.mean_reversion_permission,
            reason=decision.reason,
            source_timestamp=spy.index[-1].to_pydatetime(),
            hmm_state_probabilities=decision.hmm_state_probabilities,
        )
        return RegimeRunResult(
            True,
            decision.market_regime.value,
            decision.confidence,
            "Market regime HMM snapshot persisted.",
            decision.hmm_state_probabilities,
        )

    def _estimate_vix(self, spy) -> tuple[float, str]:
        """Approximate VIX from SPY realized volatility.

        There is no dedicated VIX feed configured, so we annualize the standard
        deviation of recent SPY returns as a volatility proxy. This lets the
        HIGH_VOLATILITY regime actually trigger during turbulent markets instead
        of being pinned to a hardcoded neutral 20.
        """
        neutral = (20.0, "VIX proxy unavailable (insufficient SPY history); defaulting to neutral 20.")
        close = spy["close"].astype(float)
        returns = close.pct_change().dropna()
        if len(returns) < 20:
            return neutral
        try:
            deltas = spy.index.to_series().diff().dropna().dt.total_seconds()
            bar_seconds = float(deltas.median()) if len(deltas) else 86400.0
        except Exception:
            bar_seconds = 86400.0
        bar_minutes = max(bar_seconds / 60.0, 1.0)
        if bar_minutes >= 240:
            periods_per_year = 252.0
        else:
            periods_per_year = 252.0 * (390.0 / bar_minutes)
        realized = float(returns.tail(60).std()) * (periods_per_year ** 0.5) * 100.0
        if not realized or realized != realized:  # guard against NaN
            return neutral
        vix_proxy = max(5.0, min(150.0, realized))
        return vix_proxy, f"VIX proxy from SPY realized volatility ~= {vix_proxy:.1f}."

    def _frame(self, symbol: str):
        for provider in ["alpaca_market_data", "yahoo_chart"]:
            frame = self.repository.clean_candles_df(symbol, provider=provider, limit=500)
            if not frame.empty:
                return frame
        return self.repository.clean_candles_df(symbol, provider="yahoo_chart", limit=0)

    def _breadth_positive(self) -> bool:
        active = self.repository.active_symbols()
        positive = seen = 0
        for symbol in active:
            frame = self._frame(symbol)
            if len(frame) < 2:
                continue
            seen += 1
            if frame["close"].iloc[-1] > frame["close"].iloc[-2]:
                positive += 1
        return seen > 0 and positive / seen >= 0.5
