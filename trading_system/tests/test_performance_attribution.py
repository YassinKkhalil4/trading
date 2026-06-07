from __future__ import annotations

from trading_system.app.attribution.performance_attribution import (
    JournalTrade,
    PerformanceAttributionService,
    StrategyMetadata,
    SymbolSectorInfo,
)
from trading_system.app.core.enums import MarketRegime


def _service() -> PerformanceAttributionService:
    return PerformanceAttributionService()


def test_strategy_attribution():
    service = _service()
    result = service.attribute(
        journal_entries=[
            JournalTrade(
                symbol="AMD",
                strategy_id="VWAP_RECLAIM",
                pnl=100.0,
                max_adverse_excursion=-50.0,
                time_in_trade_seconds=900.0,
            ),
            JournalTrade(
                symbol="NVDA",
                strategy_id="VWAP_RECLAIM",
                pnl=-30.0,
                max_adverse_excursion=-15.0,
                time_in_trade_seconds=1200.0,
            ),
            JournalTrade(
                symbol="TSLA",
                strategy_id="ORB_BREAKOUT",
                pnl=50.0,
                max_adverse_excursion=-25.0,
                time_in_trade_seconds=600.0,
            ),
        ],
        strategy_metadata={
            "VWAP_RECLAIM": StrategyMetadata("VWAP_RECLAIM", "VWAP Reclaim"),
            "ORB_BREAKOUT": StrategyMetadata("ORB_BREAKOUT", "ORB Breakout"),
        },
    )

    vwap = result.by_strategy["VWAP_RECLAIM"]
    assert vwap.trade_count == 2
    assert vwap.total_pnl == 70.0
    assert vwap.average_r == 0.0
    assert vwap.win_rate == 0.5
    assert vwap.profit_factor == 100.0 / 30.0
    assert result.by_strategy["ORB_BREAKOUT"].trade_count == 1


def test_sector_attribution():
    service = _service()
    result = service.attribute(
        journal_entries=[
            JournalTrade(
                symbol="AMD",
                pnl=80.0,
                max_adverse_excursion=-40.0,
                time_in_trade_seconds=1800.0,
            ),
            JournalTrade(
                symbol="JPM",
                pnl=-20.0,
                max_adverse_excursion=-10.0,
                time_in_trade_seconds=2400.0,
            ),
        ],
        symbol_sectors={
            "AMD": SymbolSectorInfo("Technology", "Semiconductors"),
            "JPM": SymbolSectorInfo("Financial Services", "Banks"),
        },
    )

    assert result.by_sector["Technology"].total_pnl == 80.0
    assert result.by_sector["Technology"].average_r == 2.0
    assert result.by_sector["Financial Services"].total_pnl == -20.0
    assert result.by_sector["Financial Services"].win_rate == 0.0


def test_regime_attribution():
    service = _service()
    result = service.attribute(
        journal_entries=[
            JournalTrade(
                symbol="AMD",
                market_regime=MarketRegime.BULL_TREND.value,
                pnl=40.0,
                max_adverse_excursion=-20.0,
            ),
            JournalTrade(
                symbol="AMD",
                market_regime="INVALID",
                pnl=10.0,
            ),
        ],
        regime_reference=[MarketRegime.BULL_TREND.value],
    )

    assert result.by_regime["BULL_TREND"].trade_count == 1
    assert result.by_regime["BULL_TREND"].total_pnl == 40.0
    assert result.by_regime["UNKNOWN"].trade_count == 1


def test_catalyst_attribution():
    service = _service()
    result = service.attribute(
        journal_entries=[
            JournalTrade(
                symbol="AMD",
                catalyst="evt-1",
                pnl=200.0,
                max_adverse_excursion=-100.0,
            ),
            JournalTrade(
                symbol="NVDA",
                catalyst="news_momentum",
                pnl=50.0,
                max_adverse_excursion=-25.0,
            ),
        ],
        catalyst_reference={
            "evt-1": "EARNINGS",
            "news_momentum": "NEWS_MOMENTUM",
        },
    )

    assert result.by_catalyst_type["EARNINGS"].average_r == 2.0
    assert result.by_catalyst_type["NEWS_MOMENTUM"].total_pnl == 50.0
    assert result.by_catalyst_type["NEWS_MOMENTUM"].profit_factor is None


def test_empty_data_returns_safe_empty_result():
    result = _service().attribute(journal_entries=[])

    assert result.by_strategy == {}
    assert result.by_sector == {}
    assert result.by_regime == {}
    assert result.by_catalyst_type == {}
    assert result.by_time_of_day == {}
    assert result.by_holding_period_bucket == {}
