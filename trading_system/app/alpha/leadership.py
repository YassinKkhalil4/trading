from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, select

from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository

SECTOR_ETFS = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}


@dataclass(frozen=True)
class SectorLeadershipRunResult:
    sectors_scored: int
    symbols_scored: int
    reason: str


class SectorLeadershipService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def refresh(self) -> SectorLeadershipRunResult:
        now = datetime.now(UTC)
        symbols = self.session.scalars(
            select(models.SymbolUniverse).where(models.SymbolUniverse.is_active.is_(True))
        ).all()
        sectors = sorted({row.sector for row in symbols if row.sector})
        sector_scores: dict[str, float] = {}
        spy_return = self._daily_return("SPY")
        for sector in sectors:
            sector_etf = SECTOR_ETFS.get(sector)
            members = [row for row in symbols if row.sector == sector]
            scores = [self._relative_strength(row.symbol) for row in members]
            etf_return = self._daily_return(sector_etf) if sector_etf else None
            inferred_score = round(50.0 + (sum(scores) / len(scores) if scores else 0.0) * 10.0, 2)
            etf_vs_spy_score = _etf_vs_spy_score(etf_return, spy_return)
            score = round(etf_vs_spy_score if etf_vs_spy_score is not None else inferred_score, 2)
            regime = "LEADING" if score >= 60 else "LAGGING" if score <= 40 else "NEUTRAL"
            sector_scores[sector] = score
            reason_source = (
                "actual sector ETF vs SPY"
                if etf_vs_spy_score is not None
                else "active member inferred relative strength"
            )
            self.repository.store_sector_strength_snapshot(
                sector=sector,
                sector_etf=sector_etf,
                sector_score=score,
                sector_vs_spy_score=etf_vs_spy_score
                if etf_vs_spy_score is not None
                else inferred_score,
                breadth_score=_breadth(scores),
                regime=regime,
                reason=f"{sector} scored {score:.1f} from {reason_source}.",
                payload={
                    "member_count": len(members),
                    "sector_etf_return_pct": etf_return,
                    "spy_return_pct": spy_return,
                    "inferred_member_score": inferred_score,
                    "true_sector_etf_analytics": etf_vs_spy_score is not None,
                },
                source_timestamp=now,
            )
        ranked = []
        for row in symbols:
            rs = self._relative_strength(row.symbol)
            sector_score = sector_scores.get(row.sector or "", 50.0)
            stock_return = self._daily_return(row.symbol)
            sector_return = self._daily_return(SECTOR_ETFS.get(row.sector or ""))
            stock_vs_sector = _stock_vs_sector_score(stock_return, sector_return)
            leadership_score = (
                stock_vs_sector
                if stock_vs_sector is not None
                else 50.0 + rs * 10.0 + (sector_score - 50.0) * 0.5
            )
            ranked.append(
                (
                    row,
                    rs,
                    sector_score,
                    leadership_score,
                    stock_return,
                    sector_return,
                    stock_vs_sector,
                )
            )
        ranked.sort(key=lambda item: item[3], reverse=True)
        for rank, (
            row,
            rs,
            sector_score,
            leadership_score,
            stock_return,
            sector_return,
            stock_vs_sector,
        ) in enumerate(ranked, start=1):
            self.repository.store_symbol_relative_strength_snapshot(
                symbol=row.symbol,
                sector=row.sector,
                sector_etf=SECTOR_ETFS.get(row.sector or ""),
                stock_vs_spy_score=round(50.0 + rs * 10.0, 2),
                stock_vs_sector_score=round(leadership_score, 2),
                leadership_rank=rank,
                candidate_reason=(
                    f"Rank {rank}: stock RS {rs:.2f}, sector score {sector_score:.1f}."
                ),
                payload={
                    "raw_relative_strength": rs,
                    "sector_score": sector_score,
                    "stock_return_pct": stock_return,
                    "sector_etf_return_pct": sector_return,
                    "actual_stock_vs_sector_score": stock_vs_sector,
                },
                source_timestamp=now,
            )
        return SectorLeadershipRunResult(
            sectors_scored=len(sectors),
            symbols_scored=len(ranked),
            reason="Sector leadership board refreshed from active universe features.",
        )

    def _daily_return(self, symbol: str | None) -> float | None:
        if not symbol:
            return None
        frame = self.repository.clean_candles_df(
            symbol.upper(), timeframe="1D", provider="alpaca_market_data", limit=2, valid_only=True
        )
        if frame.empty or len(frame) < 2:
            frame = self.repository.clean_candles_df(
                symbol.upper(), timeframe="1D", provider="yahoo_chart", limit=2, valid_only=True
            )
        if frame.empty or len(frame) < 2:
            return None
        previous = float(frame.iloc[-2]["close"])
        latest = float(frame.iloc[-1]["close"])
        return round(((latest - previous) / previous) * 100.0, 4) if previous > 0 else None

    def _relative_strength(self, symbol: str) -> float:
        row = self.session.scalar(
            select(models.SymbolFeatureSnapshot)
            .where(models.SymbolFeatureSnapshot.symbol == symbol.upper())
            .order_by(
                desc(models.SymbolFeatureSnapshot.source_timestamp),
                desc(models.SymbolFeatureSnapshot.created_at),
            )
            .limit(1)
        )
        if row and isinstance(row.snapshot, dict):
            return float(row.snapshot.get("relative_strength_20d") or 0.0)
        return 0.0


def _breadth(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(1 for value in values if value > 0) / len(values) * 100.0, 2)


def _etf_vs_spy_score(etf_return: float | None, spy_return: float | None) -> float | None:
    if etf_return is None or spy_return is None:
        return None
    return round(max(0.0, min(100.0, 50.0 + (etf_return - spy_return) * 5.0)), 2)


def _stock_vs_sector_score(stock_return: float | None, sector_return: float | None) -> float | None:
    if stock_return is None or sector_return is None:
        return None
    return round(max(0.0, min(100.0, 50.0 + (stock_return - sector_return) * 5.0)), 2)
