from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.ranking.expectancy import ExpectancyService, ExpectancyStats, stats_to_dict


@dataclass(frozen=True)
class ExpectancyRefreshResult:
    snapshots_created: int
    reason: str


class AlphaExpectancyRefreshService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def refresh(self) -> ExpectancyRefreshResult:
        now = datetime.now(UTC)
        view = ExpectancyService(self.repository).load()
        summary = view.summary()
        count = 0
        for bucket_type, value in summary.items():
            if bucket_type == "overall":
                self._store("overall", "overall", value, now)
                count += 1
                continue
            if not isinstance(value, dict):
                continue
            for bucket_key, stats in value.items():
                self._store(bucket_type, str(bucket_key), stats, now)
                count += 1
                if bucket_type in {"by_strategy", "by_regime", "by_catalyst_type", "by_time_of_day"}:
                    strategy_id = bucket_key if bucket_type == "by_strategy" else "ALL"
                    self.repository.store_strategy_performance_bucket(
                        strategy_id=str(strategy_id),
                        setup_type=None,
                        bucket_type=bucket_type,
                        bucket_key=str(bucket_key),
                        sample_size=stats.sample_size,
                        expectancy_r=stats.avg_r,
                        win_rate=stats.win_rate,
                        recent_expectancy_r=stats.avg_r,
                        decay_warning=bool(stats.avg_r is not None and stats.avg_r < 0),
                        confidence_level=confidence_level(stats.sample_size),
                        payload=stats_to_dict(stats),
                        source_timestamp=now,
                    )
        return ExpectancyRefreshResult(count, "Expectancy snapshots refreshed from completed trades.")

    def _store(self, bucket_type: str, bucket_key: str, stats: ExpectancyStats, now: datetime) -> None:
        self.repository.store_expectancy_snapshot(
            bucket_type=bucket_type,
            bucket_key=bucket_key,
            strategy_id=bucket_key if bucket_type == "by_strategy" else None,
            setup_type=None,
            sample_size=stats.sample_size,
            win_rate=stats.win_rate,
            average_win=None,
            average_loss=None,
            expectancy_r=stats.avg_r,
            profit_factor=None,
            max_drawdown=stats.max_drawdown,
            average_hold_seconds=stats.avg_time_to_target_seconds,
            average_slippage_bps=None,
            average_mfe=None,
            average_mae=None,
            confidence_level=confidence_level(stats.sample_size),
            payload=stats_to_dict(stats),
            source_timestamp=now,
        )


def confidence_level(sample_size: int) -> float:
    return round(min(1.0, max(0.0, sample_size / 50.0)), 4)
