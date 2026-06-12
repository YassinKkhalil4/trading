from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


DECISION_SUPPORT_PROVIDER_VERSION = "decision_support_deterministic_v1"
DECISION_SUPPORT_POLICY_VERSION = "decision_support_safety_v1"
DECISION_SUPPORT_DISCLAIMER = (
    "Decision support only. This output cannot trade, override risk, change rules, "
    "or bypass live gates."
)

UNSAFE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(place|submit|send|execute)\s+(an?\s+)?(order|trade)\b", "order intent"),
    (r"\b(buy|sell|short|cover)\s+\d+(\.\d+)?\s+(shares|contracts)\b", "sized order intent"),
    (r"\boverride\s+(risk|gate|kill switch|approval)\b", "risk or gate override"),
    (r"\b(ignore|bypass)\s+(risk|gate|live|approval|kill switch)\b", "gate bypass"),
    (r"\bchange\s+(strategy|rule|parameter|risk limit)\b", "strategy mutation"),
    (r"\b(enable|turn on)\s+live\s+trading\b", "live trading instruction"),
)


@dataclass(frozen=True)
class DecisionSupportValidation:
    accepted: bool
    reason: str
    policy_version: str = DECISION_SUPPORT_POLICY_VERSION


@dataclass(frozen=True)
class DecisionSupportArtifactPayload:
    artifact_type: str
    provider_name: str
    provider_version: str
    prompt_version: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    validation: DecisionSupportValidation
    fallback_used: bool = True

    @property
    def input_payload_hash(self) -> str:
        return payload_hash(self.input_payload)


@dataclass(frozen=True)
class ThesisSupportOutput:
    trade_type: str
    setup_quality: float
    catalyst_quality: float
    confidence: float
    reason_for_trade: str
    invalidation_reason: str
    risks: list[str]
    suggested_holding_period: str
    setup_evidence: list[str]
    catalyst_evidence: list[str]
    missing_data: list[str]
    counterarguments: list[str]
    confidence_rationale: str
    disclaimer: str = DECISION_SUPPORT_DISCLAIMER


@dataclass(frozen=True)
class TradeReviewSupportOutput:
    summary: str
    rule_adherence: str
    entry_quality: str
    exit_quality: str
    risk_discipline: str
    slippage_assessment: str
    timing_assessment: str
    mistake_tags: list[str]
    follow_up_action: str
    confidence_score: float
    disclaimer: str = DECISION_SUPPORT_DISCLAIMER


@dataclass(frozen=True)
class RecommendationSupportOutput:
    strategy_id: str | None
    recommendation: str
    severity: str
    reason: str
    supporting_metrics: dict[str, Any]
    disclaimer: str = DECISION_SUPPORT_DISCLAIMER


class DecisionSupportProvider(Protocol):
    provider_name: str
    provider_version: str

    def build_trade_thesis(self, payload: dict[str, Any]) -> ThesisSupportOutput:
        ...

    def review_trade(self, payload: dict[str, Any]) -> TradeReviewSupportOutput:
        ...

    def recommend_weekly_actions(self, payload: dict[str, Any]) -> list[RecommendationSupportOutput]:
        ...


class DeterministicDecisionSupportProvider:
    provider_name = "deterministic_decision_support"
    provider_version = DECISION_SUPPORT_PROVIDER_VERSION

    def build_trade_thesis(self, payload: dict[str, Any]) -> ThesisSupportOutput:
        symbol = str(payload.get("symbol") or "UNKNOWN").upper()
        setup_name = str(payload.get("setup_name") or "UNKNOWN_SETUP")
        scanner_reason = str(payload.get("scanner_reason") or "No scanner reason supplied.")
        catalyst_summary = payload.get("catalyst_summary")
        market_context = str(payload.get("market_context") or "Market context unavailable.")
        setup_quality = 70.0 if "accepted" in scanner_reason.lower() else 40.0
        catalyst_quality = 70.0 if catalyst_summary else 30.0
        confidence = min(85.0, (setup_quality * 0.55) + (catalyst_quality * 0.25) + 15.0)
        missing = []
        if not catalyst_summary:
            missing.append("No confirmed catalyst summary supplied.")
        return ThesisSupportOutput(
            trade_type="DAY_TRADE" if setup_name == "VWAP_RECLAIM" else "SWING",
            setup_quality=setup_quality,
            catalyst_quality=catalyst_quality,
            confidence=confidence,
            reason_for_trade=(
                f"{symbol} matched {setup_name}. {scanner_reason} "
                f"Context: {market_context}. Catalyst: {catalyst_summary or 'none confirmed'}."
            ),
            invalidation_reason="Trade invalidates if the technical level fails or catalyst context weakens.",
            risks=[
                "Decision-support thesis is explanatory only.",
                "Risk engine remains the binding authority.",
            ],
            suggested_holding_period="intraday",
            setup_evidence=[scanner_reason, market_context],
            catalyst_evidence=[str(catalyst_summary)] if catalyst_summary else [],
            missing_data=missing,
            counterarguments=[
                "Setup quality can deteriorate if market regime changes.",
                "Catalyst confidence is limited when no primary-source catalyst is linked.",
            ],
            confidence_rationale=(
                f"Confidence combines setup_quality={setup_quality:.1f}, "
                f"catalyst_quality={catalyst_quality:.1f}, and a capped deterministic base score."
            ),
        )

    def review_trade(self, payload: dict[str, Any]) -> TradeReviewSupportOutput:
        symbol = str(payload.get("symbol") or "UNKNOWN").upper()
        strategy_id = payload.get("strategy_id") or "unknown strategy"
        pnl = payload.get("pnl")
        slippage = payload.get("slippage_bps")
        time_in_trade = payload.get("time_in_trade_seconds")
        rule_violations = list(payload.get("rule_violations") or [])
        mistake_tags = list(payload.get("mistake_tags") or [])
        pnl_text = "PnL not available yet." if pnl is None else f"PnL recorded: {pnl}."
        rule_text = "No rule violations recorded." if not rule_violations else (
            f"Rule violations recorded: {', '.join(map(str, rule_violations))}."
        )
        return TradeReviewSupportOutput(
            summary=f"Decision-support review for {symbol} / {strategy_id}. {pnl_text}",
            rule_adherence=rule_text,
            entry_quality="Review entry against the stored thesis, catalyst, and regime context.",
            exit_quality="Review exit against target, stop, time-in-trade, and journal notes.",
            risk_discipline=(
                "Risk discipline needs attention." if rule_violations else "No stored risk rule breach detected."
            ),
            slippage_assessment=(
                "Slippage not available." if slippage is None else f"Average slippage recorded: {slippage} bps."
            ),
            timing_assessment=(
                "Time in trade not available."
                if time_in_trade is None
                else f"Time in trade seconds: {round(float(time_in_trade), 2)}."
            ),
            mistake_tags=[str(tag) for tag in mistake_tags],
            follow_up_action="Verify that entry, exit, risk, catalyst, and regime rules were followed.",
            confidence_score=70.0,
        )

    def recommend_weekly_actions(self, payload: dict[str, Any]) -> list[RecommendationSupportOutput]:
        metrics = dict(payload.get("metrics") or {})
        recommendations: list[RecommendationSupportOutput] = []
        if metrics.get("rule_violations", 0) > 0:
            recommendations.append(
                RecommendationSupportOutput(
                    strategy_id=None,
                    recommendation="Review rule violations and document setup conditions before increasing risk.",
                    severity="HIGH",
                    reason="Journal entries contain rule violations.",
                    supporting_metrics=metrics,
                )
            )
        if metrics.get("average_slippage_bps", 0.0) > 10:
            recommendations.append(
                RecommendationSupportOutput(
                    strategy_id=None,
                    recommendation="Review execution timing because average slippage exceeded 10 bps.",
                    severity="MEDIUM",
                    reason="Journal lifecycle metrics show elevated slippage.",
                    supporting_metrics=metrics,
                )
            )
        if metrics.get("losing_trades", 0) > metrics.get("winning_trades", 0) and metrics.get(
            "journal_entries", 0
        ) > 0:
            recommendations.append(
                RecommendationSupportOutput(
                    strategy_id=None,
                    recommendation="Review losing trade notes and tighten setup qualification criteria.",
                    severity="MEDIUM",
                    reason="Journal entries show more losing trades than winning trades.",
                    supporting_metrics=metrics,
                )
            )
        if not recommendations:
            recommendations.append(
                RecommendationSupportOutput(
                    strategy_id=None,
                    recommendation="Continue paper trading with current controls; no automatic changes applied.",
                    severity="LOW",
                    reason="No critical review issues were detected.",
                    supporting_metrics=metrics,
                )
            )
        return recommendations


def validate_decision_support_output(payload: dict[str, Any]) -> DecisionSupportValidation:
    text = json.dumps(payload, sort_keys=True, default=str).lower()
    text = text.replace(DECISION_SUPPORT_DISCLAIMER.lower(), "")
    for pattern, reason in UNSAFE_PATTERNS:
        if re.search(pattern, text):
            return DecisionSupportValidation(False, f"Rejected unsafe decision-support output: {reason}.")
    return DecisionSupportValidation(True, "Decision-support output passed structural safety validation.")


def build_artifact_payload(
    *,
    artifact_type: str,
    provider: DecisionSupportProvider,
    prompt_version: str,
    input_payload: dict[str, Any],
    output: Any,
    fallback_used: bool = True,
) -> DecisionSupportArtifactPayload:
    output_payload = asdict(output) if hasattr(output, "__dataclass_fields__") else dict(output)
    validation = validate_decision_support_output(output_payload)
    return DecisionSupportArtifactPayload(
        artifact_type=artifact_type,
        provider_name=provider.provider_name,
        provider_version=provider.provider_version,
        prompt_version=prompt_version,
        input_payload=input_payload,
        output_payload=output_payload,
        validation=validation,
        fallback_used=fallback_used,
    )


def get_decision_support_provider(provider_name: str | None = None) -> DecisionSupportProvider:
    # External providers intentionally remain disabled in v1; this boundary is the swap point.
    return DeterministicDecisionSupportProvider()


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value
