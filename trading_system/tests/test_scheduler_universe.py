from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import ProviderHealthStatus
from trading_system.app.data.collectors.alpaca_bars import ALPACA_MARKET_DATA_PROVIDER
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.services.scheduler import ScheduledCollectorRunner
from trading_system.app.services.universe import (
    MasterUniverseRefreshResult,
    MasterUniverseRefreshWorker,
    UniverseRefreshPolicy,
)
from trading_system.app.services.universe.master import UNIVERSE_ENGINE_VERSION


class StubAssetProvider:
    configured = True

    def __init__(self, assets: list[dict[str, Any]]) -> None:
        self.assets = assets

    def fetch_assets(self) -> list[dict[str, Any]]:
        return self.assets


class UnconfiguredAssetProvider:
    configured = False

    def fetch_assets(self) -> list[dict[str, Any]]:
        raise AssertionError("unconfigured provider should not be called")


class TrackingMasterWorker:
    init_calls: list[tuple[Any, dict[str, Any]]] = []
    run_calls: list[Any] = []

    def __init__(self, repository, **kwargs: Any) -> None:
        self.__class__.init_calls.append((repository, kwargs))
        self.repository = repository

    def run_once(self) -> MasterUniverseRefreshResult:
        self.__class__.run_calls.append(self.repository)
        return MasterUniverseRefreshResult(
            configured=True,
            success=True,
            fetched=1,
            upserted=1,
            tradable=1,
            disabled=0,
            rejected_payloads=0,
            reason="tracking master worker invoked",
            version=UNIVERSE_ENGINE_VERSION,
        )


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _healthy_provider(repo: TradingRepository, now: datetime) -> None:
    repo.store_provider_health_snapshot(
        provider_name=ALPACA_MARKET_DATA_PROVIDER,
        status=ProviderHealthStatus.HEALTHY.value,
        reason="provider health verified for scheduler universe test",
        reliability_score=100.0,
        source_timestamp=now,
    )


def _asset(
    symbol: str,
    name: str,
    *,
    status: str = "active",
    tradable: bool = True,
    sector: str | None = None,
    industry: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"asset-{symbol.lower()}",
        "symbol": symbol,
        "name": name,
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "status": status,
        "tradable": tradable,
        "attributes": [],
    }
    if sector:
        payload["sector"] = sector
    if industry:
        payload["industry"] = industry
    return payload


def _clean_bar(
    repo: TradingRepository,
    symbol: str,
    *,
    close: float,
    high: float,
    low: float,
    volume: float,
    source_timestamp: datetime,
) -> None:
    raw_id = repo.store_raw_candle(
        {
            "provider": ALPACA_MARKET_DATA_PROVIDER,
            "symbol": symbol,
            "timeframe": "1D",
            "source_timestamp": source_timestamp,
            "raw_payload": {"symbol": symbol, "provider": "alpaca"},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": ALPACA_MARKET_DATA_PROVIDER,
            "symbol": symbol,
            "timeframe": "1D",
            "source_timestamp": source_timestamp,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "trade_count": None,
            "vwap": close,
            "data_quality_status": "VALID",
            "quality_reason": "verified clean bar",
        }
    )


def _policy(**overrides: Any) -> UniverseRefreshPolicy:
    values = {
        "min_price": 5.0,
        "min_average_volume": 100_000.0,
        "max_spread_bps": 250.0,
        "min_universe_size": 1,
        "max_universe_size": 10,
        "lookback_bars": 3,
        "data_freshness_max_seconds": 48 * 60 * 60,
        "provider_health_max_age_seconds": 300,
    }
    values.update(overrides)
    return UniverseRefreshPolicy(**values)


def _inject_master_worker(monkeypatch, provider: StubAssetProvider | UnconfiguredAssetProvider) -> None:
    def factory(repository, **kwargs: Any) -> MasterUniverseRefreshWorker:
        return MasterUniverseRefreshWorker(
            repository,
            settings=kwargs.get("settings"),
            provider=provider,
            policy=_policy(),
        )

    monkeypatch.setattr(
        "trading_system.app.services.scheduler.MasterUniverseRefreshWorker",
        factory,
    )


def test_scheduler_universe_job_calls_master_worker(monkeypatch):
    repo = _repo()
    TrackingMasterWorker.init_calls = []
    TrackingMasterWorker.run_calls = []
    monkeypatch.setattr(
        "trading_system.app.services.scheduler.MasterUniverseRefreshWorker",
        TrackingMasterWorker,
    )

    result = ScheduledCollectorRunner(
        repo,
        settings=Settings(scheduler_use_master_universe_refresh=True),
    ).run_once("universe")

    assert result.success is True
    assert result.reason == "tracking master worker invoked"
    assert result.payload["result"]["version"] == UNIVERSE_ENGINE_VERSION
    assert len(TrackingMasterWorker.init_calls) == 1
    assert TrackingMasterWorker.init_calls[0][0] is repo
    assert len(TrackingMasterWorker.run_calls) == 1
    assert TrackingMasterWorker.run_calls[0] is repo


def test_scheduler_universe_inserts_new_symbol_via_master_worker(monkeypatch):
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "NVDA", close=130.0, high=131.0, low=129.0, volume=2_000_000, source_timestamp=now)
    _inject_master_worker(
        monkeypatch,
        StubAssetProvider([_asset("NVDA", "NVIDIA Corporation", sector="Technology")]),
    )

    result = ScheduledCollectorRunner(
        repo,
        settings=Settings(scheduler_use_master_universe_refresh=True),
    ).run_once("universe")

    rows = repo.session.scalars(select(models.SymbolUniverse)).all()
    assert result.success is True
    assert result.payload["result"]["upserted"] == 1
    assert [row.symbol for row in rows] == ["NVDA"]
    assert rows[0].is_tradable is True


def test_scheduler_universe_updates_existing_symbol_via_master_worker(monkeypatch):
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "AMD", close=100.0, high=101.0, low=99.0, volume=2_000_000, source_timestamp=now)
    _inject_master_worker(
        monkeypatch,
        StubAssetProvider([_asset("AMD", "Advanced Micro Devices, Inc.", sector="Technology")]),
    )

    runner = ScheduledCollectorRunner(repo, settings=Settings(scheduler_use_master_universe_refresh=True))
    runner.run_once("universe")

    _inject_master_worker(
        monkeypatch,
        StubAssetProvider(
            [_asset("AMD", "Advanced Micro Devices Updated", sector="Information Technology", industry="Chips")]
        ),
    )
    result = runner.run_once("universe")

    rows = repo.session.scalars(select(models.SymbolUniverse)).all()
    assert result.success is True
    assert len(rows) == 1
    assert rows[0].name == "Advanced Micro Devices Updated"
    assert rows[0].sector == "Information Technology"
    assert rows[0].industry == "Chips"


def test_scheduler_universe_marks_inactive_assets_with_disable_reason(monkeypatch):
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "XYZ", close=20.0, high=20.1, low=19.9, volume=2_000_000, source_timestamp=now)
    _inject_master_worker(
        monkeypatch,
        StubAssetProvider([_asset("XYZ", "XYZ Corp", status="inactive")]),
    )

    result = ScheduledCollectorRunner(
        repo,
        settings=Settings(scheduler_use_master_universe_refresh=True),
    ).run_once("universe")

    row = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "XYZ"))
    assert result.success is True
    assert row is not None
    assert row.is_active is False
    assert row.disable_reason == "DELISTED"
    assert result.payload["result"]["disabled"] == 1


def test_scheduler_universe_does_not_seed_fallback_assets(monkeypatch):
    repo = _repo()
    _inject_master_worker(monkeypatch, StubAssetProvider([]))

    empty_result = ScheduledCollectorRunner(
        repo,
        settings=Settings(scheduler_use_master_universe_refresh=True),
    ).run_once("universe")
    assert empty_result.success is True
    assert empty_result.payload["result"]["upserted"] == 0
    assert repo.session.scalars(select(models.SymbolUniverse)).all() == []

    _inject_master_worker(monkeypatch, UnconfiguredAssetProvider())
    unconfigured_result = ScheduledCollectorRunner(
        repo,
        settings=Settings(scheduler_use_master_universe_refresh=True),
    ).run_once("universe")
    assert unconfigured_result.success is False
    assert "not configured" in unconfigured_result.reason.lower()
    assert repo.session.scalars(select(models.SymbolUniverse)).all() == []
