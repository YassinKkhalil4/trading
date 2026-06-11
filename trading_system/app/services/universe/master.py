from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import requests
from sqlalchemy import desc, func, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DataQualityStatus, ProviderHealthStatus
from trading_system.app.data.collectors.alpaca_bars import ALPACA_MARKET_DATA_PROVIDER
from trading_system.app.data.provider_cache import ProviderRateLimiter, ProviderResponseCache
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


ALPACA_ASSET_PROVIDER = "alpaca_assets"
UNIVERSE_ENGINE_VERSION = "master_universe_engine_v1"
US_EQUITY_ASSET_CLASS = "us_equity"
VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")

INDEX_ETF_SYMBOLS = {
    "DIA",
    "IWM",
    "QQQ",
    "SPY",
    "VOO",
    "VTI",
}
SECTOR_ETF_SYMBOLS = {
    "IYR",
    "SMH",
    "XLB",
    "XLC",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLRE",
    "XLU",
    "XLV",
    "XLY",
}
COMMODITY_ETF_SYMBOLS = {
    "DBC",
    "GLD",
    "IAU",
    "SLV",
    "USO",
}
CASH_EQUIVALENT_SYMBOLS = {
    "BIL",
    "MINT",
    "SGOV",
    "SHV",
    "TBIL",
}


@dataclass(frozen=True)
class UniverseRefreshPolicy:
    min_price: float
    min_average_volume: float
    max_spread_bps: float
    min_universe_size: int = 500
    max_universe_size: int = 3_000
    lookback_bars: int = 20
    data_freshness_max_seconds: int = 36 * 60 * 60
    provider_health_max_age_seconds: int = 180

    @classmethod
    def from_settings(cls, settings: Settings) -> UniverseRefreshPolicy:
        return cls(
            min_price=settings.min_price,
            min_average_volume=float(settings.min_average_volume),
            max_spread_bps=settings.max_spread_bps,
            provider_health_max_age_seconds=settings.provider_health_max_age_seconds,
        )


@dataclass(frozen=True)
class AssetPayload:
    symbol: str
    name: str | None
    exchange: str | None
    status: str | None
    tradable: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class LiquiditySnapshot:
    symbol: str
    latest_price: float | None
    average_volume: float | None
    dollar_volume: float | None
    spread_bps: float | None
    reason: str
    disable_reason: str | None = None

    @property
    def passes(self) -> bool:
        return self.reason == "Passes liquid universe thresholds."


@dataclass(frozen=True)
class MasterUniverseRefreshResult:
    configured: bool
    success: bool
    fetched: int
    upserted: int
    tradable: int
    disabled: int
    rejected_payloads: int
    reason: str
    version: str = UNIVERSE_ENGINE_VERSION


class AssetProvider(Protocol):
    configured: bool

    def fetch_assets(self) -> list[dict[str, Any]]:
        ...


class MetadataProvider(Protocol):
    def metadata_for(self, symbol: str) -> dict[str, Any]:
        ...


class AlpacaAssetProvider:
    """Fetches Alpaca trading assets for the US equities and ETFs universe."""

    def __init__(
        self,
        settings: Settings | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_paper_api_key and self.settings.alpaca_paper_secret_key)

    def fetch_assets(self) -> list[dict[str, Any]]:
        if not self.configured:
            return []

        endpoint = f"{self.settings.alpaca_paper_base_url}/v2/assets"
        params = {"status": "all", "asset_class": US_EQUITY_ASSET_CLASS}
        request_hash = hashlib.sha256(f"{endpoint}:{params}".encode("utf-8")).hexdigest()
        cache = ProviderResponseCache(self.settings)
        cached_payload = cache.get_json(f"alpaca_assets:{request_hash}")
        if cached_payload is not None:
            return _payload_to_assets(cached_payload)

        started = time.perf_counter()
        response = self.http.get(endpoint, headers=self._headers(), params=params, timeout=30)
        duration_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        cache.set_json(f"alpaca_assets:{request_hash}", payload, ttl_seconds=60 * 60)
        return _payload_to_assets(payload)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_paper_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_paper_secret_key,
        }


class MasterUniverseRefreshWorker:
    def __init__(
        self,
        repository: TradingRepository,
        *,
        settings: Settings | None = None,
        provider: AssetProvider | None = None,
        metadata_provider: MetadataProvider | None = None,
        policy: UniverseRefreshPolicy | None = None,
        skip_liquidity: bool = False,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.provider = provider or AlpacaAssetProvider(self.settings)
        self.metadata_provider = metadata_provider
        self.policy = policy or UniverseRefreshPolicy.from_settings(self.settings)
        # News-only mode: activate every tradable US stock & ETF without running
        # the price-based liquidity screen (there is no candle data to rank on).
        self.skip_liquidity = skip_liquidity

    def run_once(self) -> MasterUniverseRefreshResult:
        if not self.provider.configured:
            return MasterUniverseRefreshResult(
                configured=False,
                success=False,
                fetched=0,
                upserted=0,
                tradable=0,
                disabled=0,
                rejected_payloads=0,
                reason="Alpaca asset provider is not configured.",
            )

        rate_limit = ProviderRateLimiter(self.repository).allow(
            provider_name=ALPACA_ASSET_PROVIDER,
            endpoint="/v2/assets",
        )
        if not rate_limit.allowed:
            return MasterUniverseRefreshResult(
                configured=True,
                success=False,
                fetched=0,
                upserted=0,
                tradable=0,
                disabled=0,
                rejected_payloads=0,
                reason=rate_limit.reason,
            )

        try:
            raw_assets = self.provider.fetch_assets()
        except requests.RequestException as exc:
            self.repository.log_api_call(
                provider=ALPACA_ASSET_PROVIDER,
                endpoint="/v2/assets",
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                success=False,
                reason=f"Alpaca assets request error: {exc}",
                duration_ms=None,
            )
            return MasterUniverseRefreshResult(
                configured=True,
                success=False,
                fetched=0,
                upserted=0,
                tradable=0,
                disabled=0,
                rejected_payloads=0,
                reason=str(exc),
            )

        assets: list[AssetPayload] = []
        rejected = 0
        for raw in raw_assets:
            parsed = _parse_asset(raw)
            if parsed is None:
                rejected += 1
            else:
                assets.append(parsed)

        now = datetime.now(UTC)
        # In news-only mode the market-data provider health is irrelevant (no price
        # pulls), so it must not disable the entire universe.
        provider_disable_reason = None if self.skip_liquidity else self._provider_disable_reason(now)
        upserted = 0
        active_count = 0
        for asset in assets:
            disable_reason = provider_disable_reason or _asset_disable_reason(asset)
            self._upsert_asset(
                asset,
                disable_reason=disable_reason,
                now=now,
                skip_liquidity=self.skip_liquidity,
            )
            upserted += 1
            if disable_reason is None:
                active_count += 1

        if self.skip_liquidity:
            # Every tradable, non-delisted US stock & ETF is part of the scan
            # universe. No price-based liquidity ranking and no giant IN clause.
            tradable = active_count
            disabled = upserted - active_count
            reason = (
                "Master universe refresh activated all tradable Alpaca US stocks & ETFs "
                "(news-only mode; price-based liquidity screen skipped)."
            )
        else:
            liquidity = self._calculate_liquidity([asset.symbol for asset in assets], now=now)
            tradable = self._apply_liquid_subset(liquidity, now=now)
            disabled = self.repository.session.scalar(
                select(func.count())
                .select_from(models.SymbolUniverse)
                .where(models.SymbolUniverse.symbol.in_([asset.symbol for asset in assets]))
                .where(models.SymbolUniverse.is_active.is_(False))
            )
            if disabled is None:
                disabled = 0

            below_minimum = (
                0 < tradable < self.policy.min_universe_size
                and len(assets) >= self.policy.min_universe_size
            )
            reason = "Master universe refresh completed from Alpaca assets and clean market data."
            if below_minimum:
                reason = (
                    "Master universe refresh completed, but the liquid subset is below the configured "
                    f"minimum of {self.policy.min_universe_size}."
                )
        return MasterUniverseRefreshResult(
            configured=True,
            success=True,
            fetched=len(raw_assets),
            upserted=upserted,
            tradable=tradable,
            disabled=int(disabled),
            rejected_payloads=rejected,
            reason=reason,
        )

    def run_forever(self, *, sleep_seconds: int | None = None) -> None:
        interval = sleep_seconds if sleep_seconds is not None else self.settings.worker_sleep_seconds
        while True:
            self.run_once()
            time.sleep(interval)

    def _provider_disable_reason(self, now: datetime) -> str | None:
        health = self.repository.latest_provider_health_for(ALPACA_MARKET_DATA_PROVIDER)
        if health is None:
            return None
        if health.status != ProviderHealthStatus.HEALTHY.value:
            return "SUSPICIOUS"
        timestamp = _aware(health.source_timestamp or health.created_at)
        if timestamp is None:
            return "SUSPICIOUS"
        age = (now - timestamp).total_seconds()
        if age > self.policy.provider_health_max_age_seconds:
            return "SUSPICIOUS"
        return None

    def _upsert_asset(
        self,
        asset: AssetPayload,
        *,
        disable_reason: str | None,
        now: datetime,
        skip_liquidity: bool = False,
    ) -> models.SymbolUniverse:
        row = self.repository.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == asset.symbol)
        )
        if row is None:
            row = models.SymbolUniverse(symbol=asset.symbol, source_timestamp=now)
            self.repository.session.add(row)

        metadata = self._metadata_for(asset.symbol, asset.raw)
        row.name = asset.name
        row.asset_class = classify_asset(asset)
        row.exchange = asset.exchange
        row.sector = _string_or_none(metadata.get("sector"))
        row.industry = _string_or_none(metadata.get("industry"))
        row.is_active = disable_reason is None
        # In news-only mode an active asset is immediately tradable for the scan
        # (no liquidity pass follows); otherwise the liquidity step decides.
        row.is_tradable = skip_liquidity and disable_reason is None
        row.is_liquid = False
        row.disable_reason = disable_reason
        row.provider_asset_id = _string_or_none(asset.raw.get("id"))
        row.provider_status = asset.status
        row.last_provider_check_at = now
        row.raw_asset_payload = asset.raw
        if disable_reason is not None:
            row.tradability_reason = f"Disabled by universe safety check: {disable_reason}."
        elif skip_liquidity:
            row.tradability_reason = (
                "News-only mode: included for news coverage without a price-based liquidity screen."
            )
        else:
            row.tradability_reason = "Awaiting liquidity calculation."
        row.change_reason = "Master universe asset sync from Alpaca assets endpoint."
        row.source_timestamp = now
        self.repository.session.commit()
        return row

    def _metadata_for(self, symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key in ("sector", "industry"):
            if raw.get(key):
                metadata[key] = raw[key]
        nested = raw.get("metadata")
        if isinstance(nested, dict):
            metadata.update({key: nested[key] for key in ("sector", "industry") if nested.get(key)})
        if self.metadata_provider is not None:
            enriched = self.metadata_provider.metadata_for(symbol)
            metadata.update(
                {key: enriched[key] for key in ("sector", "industry") if enriched.get(key)}
            )
        return metadata

    def _calculate_liquidity(
        self,
        symbols: list[str],
        *,
        now: datetime,
    ) -> dict[str, LiquiditySnapshot]:
        snapshots: dict[str, LiquiditySnapshot] = {}
        for symbol in symbols:
            snapshots[symbol] = self._snapshot_for_symbol(symbol, now=now)
        return snapshots

    def _snapshot_for_symbol(self, symbol: str, *, now: datetime) -> LiquiditySnapshot:
        rows = self.repository.session.scalars(
            select(models.CleanMarketData)
            .where(models.CleanMarketData.symbol == symbol)
            .where(models.CleanMarketData.provider == ALPACA_MARKET_DATA_PROVIDER)
            .where(models.CleanMarketData.data_quality_status == DataQualityStatus.VALID.value)
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(self.policy.lookback_bars)
        ).all()
        if not rows:
            return LiquiditySnapshot(symbol, None, None, None, None, "No fresh clean Alpaca data.", "SUSPICIOUS")

        latest = rows[0]
        latest_ts = _aware(latest.source_timestamp)
        if latest_ts is None:
            return LiquiditySnapshot(symbol, None, None, None, None, "Missing market data timestamp.", "SUSPICIOUS")
        if (now - latest_ts).total_seconds() > self.policy.data_freshness_max_seconds:
            return LiquiditySnapshot(symbol, None, None, None, None, "Clean Alpaca data is stale.", "SUSPICIOUS")

        price = float(latest.close or 0.0)
        average_volume = sum(float(row.volume or 0.0) for row in rows) / max(1, len(rows))
        dollar_volume = average_volume * price
        spread_bps = _spread_proxy_bps(float(latest.low or 0.0), float(latest.high or 0.0), price)

        if price < self.policy.min_price:
            return LiquiditySnapshot(
                symbol,
                price,
                average_volume,
                dollar_volume,
                spread_bps,
                f"Latest price {price:.2f} is below minimum {self.policy.min_price:.2f}.",
            )
        if average_volume < self.policy.min_average_volume:
            return LiquiditySnapshot(
                symbol,
                price,
                average_volume,
                dollar_volume,
                spread_bps,
                (
                    f"Average volume {average_volume:.0f} is below minimum "
                    f"{self.policy.min_average_volume:.0f}."
                ),
            )
        if spread_bps > self.policy.max_spread_bps:
            return LiquiditySnapshot(
                symbol,
                price,
                average_volume,
                dollar_volume,
                spread_bps,
                f"Spread proxy {spread_bps:.1f}bps is above maximum {self.policy.max_spread_bps:.1f}bps.",
            )
        return LiquiditySnapshot(
            symbol,
            price,
            average_volume,
            dollar_volume,
            spread_bps,
            "Passes liquid universe thresholds.",
        )

    def _apply_liquid_subset(
        self,
        snapshots: dict[str, LiquiditySnapshot],
        *,
        now: datetime,
    ) -> int:
        candidates: list[LiquiditySnapshot] = []
        for snapshot in snapshots.values():
            row = self.repository.session.scalar(
                select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == snapshot.symbol)
            )
            if row is None:
                continue
            row.latest_price = snapshot.latest_price
            row.average_volume = snapshot.average_volume
            row.dollar_volume = snapshot.dollar_volume
            row.spread_bps = snapshot.spread_bps
            row.liquidity_rank = None
            row.is_liquid = False
            if snapshot.disable_reason and row.is_active:
                row.is_active = False
                row.disable_reason = snapshot.disable_reason
            if not row.is_active:
                row.is_tradable = False
                row.tradability_reason = (
                    row.tradability_reason
                    if row.disable_reason and "Disabled by universe safety check" in (row.tradability_reason or "")
                    else snapshot.reason
                )
                continue
            if snapshot.passes:
                candidates.append(snapshot)
            else:
                row.is_tradable = False
                row.tradability_reason = snapshot.reason

        candidates.sort(
            key=lambda item: (
                -(item.dollar_volume or 0.0),
                item.spread_bps or 10_000.0,
                -(item.average_volume or 0.0),
                item.symbol,
            )
        )
        selected_symbols = {
            snapshot.symbol for snapshot in candidates[: self.policy.max_universe_size]
        }
        for rank, snapshot in enumerate(candidates, start=1):
            row = self.repository.session.scalar(
                select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == snapshot.symbol)
            )
            if row is None:
                continue
            row.liquidity_rank = rank
            if snapshot.symbol in selected_symbols:
                row.is_tradable = True
                row.is_liquid = True
                row.tradability_reason = "Included in liquid tradable universe."
            else:
                row.is_tradable = False
                row.is_liquid = False
                row.tradability_reason = (
                    f"Outside top {self.policy.max_universe_size} liquid universe ranking."
                )
            row.updated_at = now
        self.repository.session.commit()
        return len(selected_symbols)


def classify_asset(asset: AssetPayload) -> str:
    symbol = asset.symbol
    name = (asset.name or "").upper()
    raw_type = str(
        asset.raw.get("asset_type") or asset.raw.get("type") or asset.raw.get("category") or ""
    ).upper()
    is_etf = "ETF" in name or "EXCHANGE TRADED FUND" in name or raw_type == "ETF"
    if symbol in CASH_EQUIVALENT_SYMBOLS or any(term in name for term in ("TREASURY BILL", "T-BILL")):
        return "CASH_EQUIVALENT"
    if symbol in COMMODITY_ETF_SYMBOLS or any(
        term in name for term in ("GOLD", "SILVER", "OIL FUND", "COMMODITY")
    ):
        return "COMMODITY_ETF"
    if symbol in SECTOR_ETF_SYMBOLS or any(
        term in name for term in ("SECTOR", "SEMICONDUCTOR", "ENERGY SELECT", "FINANCIAL SELECT")
    ):
        return "SECTOR_ETF"
    if symbol in INDEX_ETF_SYMBOLS or any(
        term in name for term in ("S&P 500", "NASDAQ-100", "TOTAL STOCK MARKET", "DOW JONES")
    ):
        return "INDEX_ETF"
    if is_etf:
        return "ETF"
    return "EQUITY"


def _parse_asset(raw: dict[str, Any]) -> AssetPayload | None:
    asset_class = str(raw.get("asset_class") or "").lower()
    symbol = str(raw.get("symbol") or "").strip().upper()
    if asset_class and asset_class != US_EQUITY_ASSET_CLASS:
        return None
    if not VALID_SYMBOL_PATTERN.match(symbol):
        return None
    return AssetPayload(
        symbol=symbol,
        name=_string_or_none(raw.get("name")),
        exchange=_string_or_none(raw.get("exchange")),
        status=_string_or_none(raw.get("status")),
        tradable=bool(raw.get("tradable", False)),
        raw=raw,
    )


def _asset_disable_reason(asset: AssetPayload) -> str | None:
    status = (asset.status or "").lower()
    attributes = {str(item).lower() for item in asset.raw.get("attributes") or []}
    if status in {"halted", "suspended"} or "halted" in attributes:
        return "HALTED"
    if status in {"inactive", "delisted"}:
        return "DELISTED"
    if not asset.tradable:
        return "SUSPICIOUS"
    return None


def _payload_to_assets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        assets = payload.get("assets")
        if isinstance(assets, list):
            return [item for item in assets if isinstance(item, dict)]
    return []


def _spread_proxy_bps(low: float, high: float, price: float) -> float:
    if price <= 0:
        return 10_000.0
    return max(0.0, (high - low) / price * 10_000)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
