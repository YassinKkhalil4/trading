from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.core.enums import ProviderReliabilityLevel
from trading_system.app.db.seed import DEFAULT_PROVIDER_CAPABILITIES


@dataclass(frozen=True)
class ProviderCapabilityConfig:
    provider_name: str
    allowed_usage: str
    rate_limit_notes: str
    reliability_level: ProviderReliabilityLevel
    live_trading_allowed: bool
    research_allowed: bool
    intraday_allowed: bool
    enrichment_allowed: bool
    reason: str

    @classmethod
    def from_seed(cls, seed: dict) -> "ProviderCapabilityConfig":
        return cls(
            provider_name=seed["provider_name"],
            allowed_usage=seed["allowed_usage"],
            rate_limit_notes=seed.get("rate_limit_notes", ""),
            reliability_level=ProviderReliabilityLevel(seed["reliability_level"]),
            live_trading_allowed=bool(seed.get("live_trading_allowed", False)),
            research_allowed=bool(seed.get("research_allowed", False)),
            intraday_allowed=bool(seed.get("intraday_allowed", False)),
            enrichment_allowed=bool(seed.get("enrichment_allowed", False)),
            reason=seed.get("reason", ""),
        )


DEFAULT_CAPABILITY_MAP = {
    item["provider_name"]: ProviderCapabilityConfig.from_seed(item)
    for item in DEFAULT_PROVIDER_CAPABILITIES
}


class ProviderCapabilityError(RuntimeError):
    pass


def get_provider_capability(provider_name: str) -> ProviderCapabilityConfig:
    try:
        return DEFAULT_CAPABILITY_MAP[provider_name]
    except KeyError as exc:
        raise ProviderCapabilityError(f"Unknown provider: {provider_name}") from exc


def assert_provider_usage(
    provider_name: str,
    *,
    research: bool = False,
    intraday: bool = False,
    enrichment: bool = False,
    live_trading: bool = False,
) -> ProviderCapabilityConfig:
    capability = get_provider_capability(provider_name)
    if research and not capability.research_allowed:
        raise ProviderCapabilityError(f"{provider_name} is not approved for research: {capability.reason}")
    if intraday and not capability.intraday_allowed:
        raise ProviderCapabilityError(
            f"{provider_name} is not approved for intraday collection: {capability.reason}"
        )
    if enrichment and not capability.enrichment_allowed:
        raise ProviderCapabilityError(
            f"{provider_name} is not approved for enrichment: {capability.reason}"
        )
    if live_trading and not capability.live_trading_allowed:
        raise ProviderCapabilityError(
            f"{provider_name} is not approved for live trading: {capability.reason}"
        )
    return capability


def list_default_provider_capabilities() -> list[ProviderCapabilityConfig]:
    return list(DEFAULT_CAPABILITY_MAP.values())
