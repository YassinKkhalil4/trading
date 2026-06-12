from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_system.app.ai.decision_support import (
    build_artifact_payload,
    get_decision_support_provider,
)


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


def build_decision_support_thesis(
    *,
    symbol: str,
    setup_name: str,
    scanner_reason: str,
    catalyst_summary: str | None,
    market_context: str,
) -> tuple[AIThesis, dict[str, Any], Any]:
    provider = get_decision_support_provider()
    input_payload = {
        "symbol": symbol,
        "setup_name": setup_name,
        "scanner_reason": scanner_reason,
        "catalyst_summary": catalyst_summary,
        "market_context": market_context,
    }
    output = provider.build_trade_thesis(input_payload)
    artifact = build_artifact_payload(
        artifact_type="TRADE_THESIS",
        provider=provider,
        prompt_version=AI_THESIS_PROMPT_VERSION,
        input_payload=input_payload,
        output=output,
        fallback_used=True,
    )
    if not artifact.validation.accepted:
        output = provider.build_trade_thesis({**input_payload, "catalyst_summary": None})
        artifact = build_artifact_payload(
            artifact_type="TRADE_THESIS",
            provider=provider,
            prompt_version=AI_THESIS_PROMPT_VERSION,
            input_payload=input_payload,
            output=output,
            fallback_used=True,
        )
    thesis = AIThesis(
        trade_type=output.trade_type,
        setup_quality=output.setup_quality,
        catalyst_quality=output.catalyst_quality,
        confidence=output.confidence,
        reason_for_trade=output.reason_for_trade,
        invalidation_reason=output.invalidation_reason,
        risks=output.risks,
        suggested_holding_period=output.suggested_holding_period,
    )
    return thesis, artifact.output_payload, artifact
