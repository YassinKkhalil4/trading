from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeMonitorDecision:
    action: str
    reason: str


def evaluate_day_trade_to_swing_conversion(
    *,
    profitable: bool,
    close_near_high_of_day: bool,
    volume_confirms: bool,
    catalyst_still_valid: bool,
    overnight_risk_approved: bool,
    market_regime_supportive: bool,
) -> TradeMonitorDecision:
    if not profitable:
        return TradeMonitorDecision("block_conversion", "Never convert a losing day trade into a swing trade.")
    if not close_near_high_of_day:
        return TradeMonitorDecision("block_conversion", "Close is not near high of day.")
    if not volume_confirms:
        return TradeMonitorDecision("block_conversion", "Volume does not confirm conversion.")
    if not catalyst_still_valid:
        return TradeMonitorDecision("block_conversion", "Catalyst thesis is no longer valid.")
    if not overnight_risk_approved:
        return TradeMonitorDecision("block_conversion", "Risk engine rejected overnight exposure.")
    if not market_regime_supportive:
        return TradeMonitorDecision("block_conversion", "Market regime is not supportive.")
    return TradeMonitorDecision("convert_to_swing", "Conversion criteria passed.")

