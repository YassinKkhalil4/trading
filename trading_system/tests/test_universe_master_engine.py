from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.enums import ProviderHealthStatus
from trading_system.app.data.collectors.alpaca_bars import ALPACA_MARKET_DATA_PROVIDER
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.services.universe import MasterUniverseRefreshWorker, UniverseRefreshPolicy


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


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _healthy_provider(repo: TradingRepository, now: datetime) -> None:
    repo.store_provider_health_snapshot(
        provider_name=ALPACA_MARKET_DATA_PROVIDER,
        status=ProviderHealthStatus.HEALTHY.value,
        reason="provider health verified for universe test",
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
    attributes: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"asset-{symbol.lower()}",
        "symbol": symbol,
        "name": name,
        "asset_class": "us_equity",
        "exchange": "NASDAQ" if symbol == "AMD" else "NYSEARCA",
        "status": status,
        "tradable": tradable,
        "attributes": attributes or [],
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
        "max_universe_size": 3,
        "lookback_bars": 3,
        "data_freshness_max_seconds": 48 * 60 * 60,
        "provider_health_max_age_seconds": 300,
    }
    values.update(overrides)
    return UniverseRefreshPolicy(**values)


def test_master_universe_sync_classifies_metadata_and_ranks_liquid_subset():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    for symbol, close, volume in [
        ("AMD", 100.0, 2_000_000),
        ("SPY", 500.0, 800_000),
        ("XLE", 90.0, 1_000_000),
        ("GLD", 180.0, 1_500_000),
        ("BIL", 91.0, 300_000),
    ]:
        _clean_bar(repo, symbol, close=close, high=close * 1.005, low=close * 0.995, volume=volume, source_timestamp=now)

    provider = StubAssetProvider(
        [
            _asset("AMD", "Advanced Micro Devices, Inc.", sector="Information Technology", industry="Semiconductors"),
            _asset("SPY", "SPDR S&P 500 ETF Trust"),
            _asset("XLE", "Energy Select Sector SPDR Fund", sector="Energy"),
            _asset("GLD", "SPDR Gold Trust"),
            _asset("BIL", "SPDR Bloomberg 1-3 Month T-Bill ETF"),
            {"symbol": "BAD$", "asset_class": "us_equity", "name": "Invalid symbol", "status": "active", "tradable": True},
        ]
    )

    result = MasterUniverseRefreshWorker(repo, provider=provider, policy=_policy()).run_once()

    assert result.success is True
    assert result.fetched == 6
    assert result.upserted == 5
    assert result.rejected_payloads == 1
    assert result.tradable == 3

    rows = {
        row.symbol: row
        for row in repo.session.scalars(select(models.SymbolUniverse)).all()
    }
    assert set(rows) == {"AMD", "SPY", "XLE", "GLD", "BIL"}
    assert rows["AMD"].asset_class == "EQUITY"
    assert rows["AMD"].sector == "Information Technology"
    assert rows["AMD"].industry == "Semiconductors"
    assert rows["SPY"].asset_class == "INDEX_ETF"
    assert rows["XLE"].asset_class == "SECTOR_ETF"
    assert rows["GLD"].asset_class == "COMMODITY_ETF"
    assert rows["BIL"].asset_class == "CASH_EQUIVALENT"

    tradable_symbols = {symbol for symbol, row in rows.items() if row.is_tradable}
    assert tradable_symbols == {"AMD", "SPY", "GLD"}
    assert rows["SPY"].liquidity_rank == 1
    assert rows["GLD"].liquidity_rank == 2
    assert rows["AMD"].liquidity_rank == 3
    assert rows["XLE"].is_active is True
    assert rows["XLE"].is_tradable is False
    assert "Outside top 3" in rows["XLE"].tradability_reason


def test_master_universe_updates_existing_rows_and_records_safety_disables():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "AMD", close=100.0, high=100.5, low=99.5, volume=2_000_000, source_timestamp=now)
    _clean_bar(repo, "MSFT", close=420.0, high=421.0, low=419.0, volume=2_000_000, source_timestamp=now - timedelta(days=5))
    _clean_bar(repo, "QQQ", close=450.0, high=451.0, low=449.0, volume=2_000_000, source_timestamp=now)

    first_provider = StubAssetProvider([_asset("AMD", "Advanced Micro Devices, Inc.")])
    first = MasterUniverseRefreshWorker(repo, provider=first_provider, policy=_policy()).run_once()
    assert first.success is True
    amd = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "AMD"))
    assert amd is not None
    assert amd.is_active is True
    assert amd.is_tradable is True

    second_provider = StubAssetProvider(
        [
            _asset("AMD", "Advanced Micro Devices Updated", status="inactive"),
            _asset("MSFT", "Microsoft Corporation"),
            _asset("QQQ", "Invesco QQQ Trust", attributes=["halted"]),
        ]
    )
    second = MasterUniverseRefreshWorker(repo, provider=second_provider, policy=_policy()).run_once()

    assert second.success is True
    rows = {
        row.symbol: row
        for row in repo.session.scalars(select(models.SymbolUniverse)).all()
    }
    assert rows["AMD"].name == "Advanced Micro Devices Updated"
    assert rows["AMD"].is_active is False
    assert rows["AMD"].disable_reason == "DELISTED"
    assert rows["MSFT"].is_active is False
    assert rows["MSFT"].disable_reason == "SUSPICIOUS"
    assert rows["MSFT"].tradability_reason == "Clean Alpaca data is stale."
    assert rows["QQQ"].is_active is False
    assert rows["QQQ"].disable_reason == "HALTED"
    assert rows["QQQ"].is_tradable is False


def test_master_universe_inserts_new_asset_without_static_fallback_data():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "NVDA", close=130.0, high=131.0, low=129.0, volume=2_000_000, source_timestamp=now)

    result = MasterUniverseRefreshWorker(
        repo,
        provider=StubAssetProvider([_asset("NVDA", "NVIDIA Corporation", sector="Technology")]),
        policy=_policy(),
    ).run_once()

    rows = repo.session.scalars(select(models.SymbolUniverse)).all()
    assert result.success is True
    assert result.upserted == 1
    assert [row.symbol for row in rows] == ["NVDA"]
    assert rows[0].is_tradable is True


def test_master_universe_updates_existing_asset_metadata_cleanly():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "AMD", close=100.0, high=101.0, low=99.0, volume=2_000_000, source_timestamp=now)

    worker = MasterUniverseRefreshWorker(
        repo,
        provider=StubAssetProvider([_asset("AMD", "Advanced Micro Devices, Inc.", sector="Technology")]),
        policy=_policy(),
    )
    worker.run_once()
    worker.provider = StubAssetProvider(
        [_asset("AMD", "Advanced Micro Devices Updated", sector="Information Technology", industry="Chips")]
    )
    result = worker.run_once()

    rows = repo.session.scalars(select(models.SymbolUniverse)).all()
    assert result.upserted == 1
    assert len(rows) == 1
    assert rows[0].name == "Advanced Micro Devices Updated"
    assert rows[0].sector == "Information Technology"
    assert rows[0].industry == "Chips"


def test_master_universe_disables_delisted_or_inactive_asset_and_persists_reason():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    _clean_bar(repo, "XYZ", close=20.0, high=20.1, low=19.9, volume=2_000_000, source_timestamp=now)

    result = MasterUniverseRefreshWorker(
        repo,
        provider=StubAssetProvider([_asset("XYZ", "XYZ Corp", status="inactive")]),
        policy=_policy(),
    ).run_once()

    row = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "XYZ"))
    assert result.disabled == 1
    assert row is not None
    assert row.is_active is False
    assert row.disable_reason == "DELISTED"
    assert row.tradability_reason == "Disabled by universe safety check: DELISTED."


def test_master_universe_classifies_generic_index_commodity_and_sector_etfs():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    assets = [
        _asset("ARKK", "ARK Innovation ETF"),
        _asset("SPY", "SPDR S&P 500 ETF Trust"),
        _asset("GLD", "SPDR Gold Trust"),
        _asset("SLV", "iShares Silver Trust"),
        _asset("XLK", "Technology Select Sector SPDR Fund"),
        _asset("XLE", "Energy Select Sector SPDR Fund"),
    ]
    for idx, asset in enumerate(assets):
        _clean_bar(
            repo,
            asset["symbol"],
            close=100.0 + idx,
            high=101.0 + idx,
            low=99.0 + idx,
            volume=2_000_000,
            source_timestamp=now,
        )

    MasterUniverseRefreshWorker(repo, provider=StubAssetProvider(assets), policy=_policy(max_universe_size=10)).run_once()

    rows = {
        row.symbol: row.asset_class
        for row in repo.session.scalars(select(models.SymbolUniverse)).all()
    }
    assert rows["ARKK"] == "ETF"
    assert rows["SPY"] == "INDEX_ETF"
    assert rows["GLD"] == "COMMODITY_ETF"
    assert rows["SLV"] == "COMMODITY_ETF"
    assert rows["XLK"] == "SECTOR_ETF"
    assert rows["XLE"] == "SECTOR_ETF"


def test_master_universe_liquidity_filter_includes_and_excludes_by_thresholds():
    repo = _repo()
    now = datetime.now(UTC)
    _healthy_provider(repo, now)
    for symbol, close, high, low, volume in [
        ("LIQD", 50.0, 50.2, 49.8, 2_000_000),
        ("CHEAP", 4.0, 4.1, 3.9, 2_000_000),
        ("THIN", 60.0, 60.2, 59.8, 50_000),
        ("WIDE", 70.0, 75.0, 65.0, 2_000_000),
    ]:
        _clean_bar(repo, symbol, close=close, high=high, low=low, volume=volume, source_timestamp=now)

    MasterUniverseRefreshWorker(
        repo,
        provider=StubAssetProvider(
            [
                _asset("LIQD", "Liquid Corp"),
                _asset("CHEAP", "Cheap Corp"),
                _asset("THIN", "Thin Corp"),
                _asset("WIDE", "Wide Spread Corp"),
            ]
        ),
        policy=_policy(max_universe_size=10, max_spread_bps=100.0),
    ).run_once()

    rows = {
        row.symbol: row
        for row in repo.session.scalars(select(models.SymbolUniverse)).all()
    }
    assert rows["LIQD"].is_tradable is True
    assert rows["CHEAP"].is_tradable is False
    assert "below minimum" in rows["CHEAP"].tradability_reason
    assert rows["THIN"].is_tradable is False
    assert "Average volume" in rows["THIN"].tradability_reason
    assert rows["WIDE"].is_tradable is False
    assert "Spread proxy" in rows["WIDE"].tradability_reason


def test_master_universe_empty_or_unconfigured_provider_does_not_seed_fallback_assets():
    repo = _repo()
    configured_empty = MasterUniverseRefreshWorker(
        repo,
        provider=StubAssetProvider([]),
        policy=_policy(),
    ).run_once()
    unconfigured = MasterUniverseRefreshWorker(
        repo,
        provider=UnconfiguredAssetProvider(),
        policy=_policy(),
    ).run_once()

    rows = repo.session.scalars(select(models.SymbolUniverse)).all()
    assert configured_empty.success is True
    assert configured_empty.upserted == 0
    assert unconfigured.success is False
    assert rows == []
