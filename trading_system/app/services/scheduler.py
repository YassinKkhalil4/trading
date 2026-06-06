from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from trading_system.app.core.enums import EnvironmentMode
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.catalysts.catalyst_engine import CatalystEngine
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector
from trading_system.app.data.collectors.news_rss import NewsRssCollector
from trading_system.app.data.collectors.sec_edgar import SecEdgarCollector
from trading_system.app.data.collectors.yahoo_chart import YahooChartCollector
from trading_system.app.data.quality_repair import MissingCandleRepairService
from trading_system.app.data.universe import LiquidUniverseBuilder
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.execution.fill_reconciliation import FillReconciliationLoop
from trading_system.app.features.production_features import ProductionFeatureEngine
from trading_system.app.journal.review_engine import TradeReviewEngine
from trading_system.app.learning.recommendations import LearningRecommendationEngine
from trading_system.app.monitoring.trade_monitor_service import TradeMonitorService
from trading_system.app.ops.coordination import CoordinationLockManager
from trading_system.app.ops.provider_health import ProviderHealthService
from trading_system.app.regime.regime_service import MarketRegimeService
from trading_system.app.risk.live_readiness import LiveReadinessService
from trading_system.app.scanners.production_scanners import ProductionScannerEngine


SCHEDULER_VERSION = "scheduled_collector_runner_v1"


@dataclass(frozen=True)
class ScheduledJobResult:
    job_name: str
    success: bool
    reason: str
    payload: dict[str, Any]
    version: str = SCHEDULER_VERSION


class ScheduledCollectorRunner:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def run_once(
        self,
        job_name: str,
        *,
        symbols: list[str] | None = None,
        actor: str = "system",
    ) -> ScheduledJobResult:
        normalized_job = job_name.strip().lower()
        if normalized_job == "all":
            payload = {}
            success = True
            reasons = []
            for child in [
                "market_data",
                "features",
                "regime",
                "news",
                "sec",
                "catalysts",
                "production_scanners",
                "provider_health",
                "universe",
                "missing_candle_repair",
                "live_readiness",
                "fill_reconciliation",
                "trade_monitor",
                "reviews",
                "learning",
            ]:
                result = self.run_once(child, symbols=symbols, actor=actor)
                payload[child] = asdict(result)
                success = success and result.success
                reasons.append(f"{child}: {result.reason}")
            return ScheduledJobResult("all", success, " | ".join(reasons), payload)

        started = datetime.now(UTC)
        lock_key = f"scheduler:{normalized_job}"
        lock_manager = CoordinationLockManager(self.settings)
        lock = lock_manager.acquire(
            lock_key,
            ttl_seconds=self.settings.scheduler_lock_ttl_seconds,
        )
        try:
            if not lock.acquired:
                result = ScheduledJobResult(
                    job_name=normalized_job,
                    success=True,
                    reason=f"Scheduled job skipped because another worker holds {lock.key}.",
                    payload={"skipped": True, "lock": lock.__dict__},
                )
            else:
                result = self._run_job(normalized_job, symbols=symbols, actor=actor)
        except Exception as exc:
            result = ScheduledJobResult(
                job_name=normalized_job,
                success=False,
                reason=f"Scheduled job failed: {exc}",
                payload={"lock": lock.__dict__},
            )
        finally:
            released = lock_manager.release(lock)
        finished = datetime.now(UTC)
        payload = {**result.payload, "coordination_lock": lock.__dict__, "lock_released": released}
        result = ScheduledJobResult(result.job_name, result.success, result.reason, payload)
        self.repository.store_scheduler_run(
            job_name=normalized_job,
            success=result.success,
            started_at=started,
            finished_at=finished,
            reason=result.reason,
            payload=result.payload,
        )
        return result

    def run_forever(self) -> None:
        while True:
            self.run_once("market_data")
            self.run_once("features")
            self.run_once("regime")
            self.run_once("catalysts")
            self.run_once("production_scanners")
            self.run_once("provider_health")
            self.run_once("universe")
            self.run_once("missing_candle_repair")
            self.run_once("fill_reconciliation")
            self.run_once("trade_monitor")
            self.run_once("news")
            self.run_once("sec")
            time.sleep(max(5, min(_cadences(self.settings))))

    def _run_job(
        self,
        job_name: str,
        *,
        symbols: list[str] | None,
        actor: str,
    ) -> ScheduledJobResult:
        symbols = symbols or self.repository.active_symbols()
        if job_name == "market_data":
            alpaca = AlpacaBarsCollector(self.repository, self.settings)
            yahoo = YahooChartCollector(self.repository)
            results = []
            for symbol in symbols:
                result = alpaca.collect(symbol)
                if not result.success:
                    if self.settings.environment_mode == EnvironmentMode.RESEARCH:
                        fallback = yahoo.collect(symbol)
                        results.append(
                            {
                                "primary": result.__dict__,
                                "fallback": fallback.__dict__,
                                "success": fallback.candles_seen > 0,
                                "fallback_allowed": True,
                            }
                        )
                    else:
                        results.append(
                            {
                                "primary": result.__dict__,
                                "fallback": None,
                                "success": False,
                                "fallback_allowed": False,
                                "reason": "Yahoo fallback is research-only and blocked outside research mode.",
                            }
                        )
                else:
                    results.append(
                        {
                            "primary": result.__dict__,
                            "fallback": None,
                            "success": True,
                            "fallback_allowed": False,
                        }
                    )
            success = any(item["success"] for item in results)
            fallback_mode = "Yahoo research fallback allowed." if self.settings.environment_mode == EnvironmentMode.RESEARCH else "Yahoo fallback blocked outside research mode."
            return ScheduledJobResult(
                job_name,
                success,
                f"Scheduled market data collection completed with Alpaca primary. {fallback_mode}",
                {"results": results, "symbols": symbols},
            )
        if job_name == "features":
            result = ProductionFeatureEngine(self.repository).run_once(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "regime":
            result = MarketRegimeService(self.repository).run_once()
            return ScheduledJobResult(job_name, result.computed, result.reason, {"result": asdict(result)})
        if job_name == "news":
            result = NewsRssCollector(self.repository, self.settings).collect(symbols)
            return ScheduledJobResult(job_name, result.success, result.reason, {"result": asdict(result)})
        if job_name == "sec":
            result = SecEdgarCollector(self.repository, self.settings).collect(symbols)
            return ScheduledJobResult(job_name, result.success, result.reason, {"result": asdict(result)})
        if job_name == "catalysts":
            result = CatalystEngine(self.repository).run_once(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "production_scanners":
            result = ProductionScannerEngine(self.repository).run_once(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "provider_health":
            result = ProviderHealthService(self.repository, self.settings).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "universe":
            result = LiquidUniverseBuilder(self.repository, self.settings).refresh(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "missing_candle_repair":
            result = MissingCandleRepairService(self.repository, self.settings).run_once(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "live_readiness":
            result = LiveReadinessService(self.repository, self.settings).generate_report(actor=actor)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "fill_reconciliation":
            result = FillReconciliationLoop(self.repository, self.settings).run_once()
            return ScheduledJobResult(job_name, result.success, result.reason, {"result": asdict(result)})
        if job_name == "trade_monitor":
            result = TradeMonitorService(self.repository).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "reviews":
            result = TradeReviewEngine(self.repository).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "learning":
            result = LearningRecommendationEngine(self.repository).run_weekly_review()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        raise ValueError(f"Unknown scheduled job: {job_name}")


def _cadences(settings: Settings) -> list[int]:
    return [
        settings.scheduler_market_data_seconds,
        settings.scheduler_fill_reconciliation_seconds,
        settings.scheduler_news_seconds,
        settings.scheduler_sec_seconds,
        settings.scheduler_regime_seconds,
        settings.scheduler_catalyst_seconds,
        settings.scheduler_trade_monitor_seconds,
        settings.scheduler_review_seconds,
    ]
