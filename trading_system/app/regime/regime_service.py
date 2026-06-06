from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.core.enums import MarketRegime
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.regime.market_regime_engine import RegimeInputs, classify_market_regime


REGIME_SERVICE_VERSION = "regime_service_v1"


@dataclass(frozen=True)
class RegimeRunResult:
    computed: bool
    market_regime: str | None
    confidence: float | None
    reason: str
    version: str = REGIME_SERVICE_VERSION


class MarketRegimeService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self) -> RegimeRunResult:
        spy = self._frame("SPY")
        qqq = self._frame("QQQ")
        if len(spy) < 50 or len(qqq) < 20:
            return RegimeRunResult(
                False,
                None,
                None,
                "Not enough SPY/QQQ clean candles to compute production regime.",
            )
        spy_close = spy["close"]
        qqq_close = qqq["close"]
        spy_20 = spy_close.rolling(20, min_periods=1).mean().iloc[-1]
        spy_50 = spy_close.rolling(50, min_periods=1).mean().iloc[-1]
        qqq_20 = qqq_close.rolling(20, min_periods=1).mean().iloc[-1]
        breadth_positive = self._breadth_positive()
        decision = classify_market_regime(
            RegimeInputs(
                spy_above_20ma=bool(spy_close.iloc[-1] > spy_20),
                spy_above_50ma=bool(spy_close.iloc[-1] > spy_50),
                qqq_above_20ma=bool(qqq_close.iloc[-1] > qqq_20),
                vix_level=20.0,
                breadth_positive=breadth_positive,
            )
        )
        self.repository.store_market_regime_snapshot(
            market_regime=decision.market_regime.value,
            confidence=decision.confidence,
            allowed_bias=decision.allowed_bias,
            risk_multiplier=decision.risk_multiplier,
            breakout_permission=decision.breakout_permission,
            mean_reversion_permission=decision.mean_reversion_permission,
            reason=f"{decision.reason} VIX input defaults to neutral 20 until VIX feed is configured.",
            source_timestamp=spy.index[-1].to_pydatetime(),
        )
        return RegimeRunResult(
            True,
            decision.market_regime.value,
            decision.confidence,
            "Market regime snapshot persisted.",
        )

    def _frame(self, symbol: str):
        for provider in ["alpaca_market_data", "yahoo_chart"]:
            frame = self.repository.clean_candles_df(symbol, provider=provider, limit=200)
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
