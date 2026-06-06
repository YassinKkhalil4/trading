from __future__ import annotations

from dataclasses import dataclass


AI_THESIS_PROMPT_VERSION = "ai_thesis_prompt_v1"


@dataclass(frozen=True)
class AIThesis:
    trade_type: str
    setup_quality: float
    catalyst_quality: float
    confidence: float
    reason_for_trade: str
    invalidation_reason: str
    risks: list[str]
    suggested_holding_period: str
    prompt_version: str = AI_THESIS_PROMPT_VERSION


def build_rule_based_thesis(
    *,
    symbol: str,
    setup_name: str,
    scanner_reason: str,
    catalyst_summary: str | None,
    market_context: str,
) -> AIThesis:
    catalyst_quality = 70.0 if catalyst_summary else 30.0
    setup_quality = 70.0 if "accepted" in scanner_reason.lower() else 40.0
    confidence = min(85.0, (setup_quality * 0.55) + (catalyst_quality * 0.25) + 15.0)
    return AIThesis(
        trade_type="DAY_TRADE" if setup_name == "VWAP_RECLAIM" else "SWING",
        setup_quality=setup_quality,
        catalyst_quality=catalyst_quality,
        confidence=confidence,
        reason_for_trade=(
            f"{symbol} matched {setup_name}. {scanner_reason} "
            f"Context: {market_context}. Catalyst: {catalyst_summary or 'none confirmed'}."
        ),
        invalidation_reason="Trade invalidates if the technical level fails or catalyst context weakens.",
        risks=["AI thesis is explanatory only.", "Risk engine remains the binding authority."],
        suggested_holding_period="intraday",
    )

