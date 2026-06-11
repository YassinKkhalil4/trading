from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

from trading_system.app.core.enums import EnvironmentMode, SessionStatus
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.data.market_calendar import get_session, to_eastern
from trading_system.app.catalysts.catalyst_engine import CatalystEngine
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector
from trading_system.app.data.collectors.alpha_vantage_news import AlphaVantageNewsCollector
from trading_system.app.data.collectors.sec_edgar import SecEdgarCollector
from trading_system.app.data.collectors.yahoo_chart import YahooChartCollector
from trading_system.app.data.quality_repair import MissingCandleRepairService
from trading_system.app.data.universe import LiquidUniverseBuilder
from trading_system.app.services.universe import MasterUniverseRefreshWorker
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
from trading_system.app.scanners.news_screener import NewsOpportunityScanner
from trading_system.app.scanners.production_scanners import ProductionScannerEngine


SCHEDULER_VERSION = "scheduled_collector_runner_v1"

# Jobs that depend on stock-market (price/candle) data. In news-only mode the
# platform pulls only Alpha Vantage news, so these are skipped entirely.
_PRICE_ONLY_JOBS = frozenset(
    {
        "market_data",
        "features",
        "production_scanners",
        "missing_candle_repair",
        "regime",
        "sec",
    }
)


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
            cadences = _job_cadences(self.settings)
            now = datetime.now(UTC)
            last_runs = self.repository.last_scheduler_run_times()
            children = [
                "market_data",
                "features",
                "regime",
                "news",
                "sec",
                "catalysts",
                "production_scanners",
                "provider_health",
                "universe",
                "news_screener",
                "missing_candle_repair",
                "live_readiness",
                "fill_reconciliation",
                "trade_monitor",
                "reviews",
                "learning",
            ]
            if self.settings.news_only_mode:
                children = [name for name in children if name not in _PRICE_ONLY_JOBS]
            else:
                children = [name for name in children if name != "news_screener"]
            for child in children:
                if child == "news":
                    due, news_reason = news_pull_due(now, last_runs.get("news"), self.settings)
                    if not due:
                        payload["news"] = {"skipped": True, "reason": news_reason}
                        reasons.append(f"news: skipped ({news_reason})")
                        continue
                    result = self.run_once("news", symbols=symbols, actor=actor)
                    payload["news"] = asdict(result)
                    success = success and result.success
                    reasons.append(f"news: {result.reason}")
                    continue
                cadence = cadences.get(child)
                if cadence is not None and not _cadence_elapsed(now, last_runs.get(child), cadence):
                    payload[child] = {"skipped": True, "reason": f"Cadence of {cadence}s has not elapsed."}
                    reasons.append(f"{child}: skipped (cadence not elapsed)")
                    continue
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
        cadences = _job_cadences(self.settings)
        last_run: dict[str, float] = {}
        while True:
            now = time.monotonic()
            for job_name, cadence in cadences.items():
                if job_name == "news":
                    due, _reason = news_pull_due(
                        datetime.now(UTC),
                        self.repository.last_scheduler_run_times().get("news"),
                        self.settings,
                    )
                    if due:
                        self.run_once("news")
                        last_run["news"] = time.monotonic()
                    continue
                previous = last_run.get(job_name)
                if previous is None or (now - previous) >= cadence:
                    self.run_once(job_name)
                    last_run[job_name] = time.monotonic()
            now = time.monotonic()
            candidates = [
                (last_run[job_name] + cadence) - now
                for job_name, cadence in cadences.items()
                if job_name in last_run
            ]
            # Cap the sleep so the market-aware news schedule is re-evaluated at
            # least once a minute (to catch premarket/session transitions).
            next_due = min([*candidates, 60.0]) if candidates else 60.0
            time.sleep(max(1.0, next_due))

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
            result = AlphaVantageNewsCollector(self.repository, self.settings).collect(symbols)
            return ScheduledJobResult(job_name, result.success, result.reason, {"result": asdict(result)})
        if job_name == "sec":
            result = SecEdgarCollector(self.repository, self.settings).collect(symbols)
            return ScheduledJobResult(job_name, result.success, result.reason, {"result": asdict(result)})
        if job_name == "catalysts":
            # Pass no symbols so the engine scopes by an active-universe subquery
            # instead of a ~13k-item IN clause (news is already universe-filtered).
            result = CatalystEngine(self.repository).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "production_scanners":
            result = ProductionScannerEngine(self.repository).run_once(symbols)
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "news_screener":
            result = NewsOpportunityScanner(self.repository, self.settings).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "provider_health":
            result = ProviderHealthService(self.repository, self.settings).run_once()
            return ScheduledJobResult(job_name, True, result.reason, {"result": asdict(result)})
        if job_name == "universe":
            if self.settings.scheduler_use_master_universe_refresh:
                result = MasterUniverseRefreshWorker(
                    self.repository,
                    settings=self.settings,
                    skip_liquidity=self.settings.news_only_mode,
                ).run_once()
                return ScheduledJobResult(
                    job_name,
                    result.success,
                    result.reason,
                    {"result": asdict(result)},
                )
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


def _ran_on_eastern_date(last_run: datetime | None, session_date: date) -> bool:
    if last_run is None:
        return False
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    return to_eastern(last_run).date() == session_date


def news_pull_due(
    now: datetime,
    last_run: datetime | None,
    settings: Settings,
) -> tuple[bool, str]:
    """Decide whether a news pull should run at ``now``.

    News is collected on a market-aware schedule: one pull each morning during
    premarket (before the open) and then a configurable number of pulls spread
    evenly across the 6.5-hour regular session. Outside those windows
    (after-hours, overnight, weekends, holidays) news pulls are paused so the
    rate-limited Alpha Vantage budget is spent when news actually moves.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if last_run is not None and last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)

    session = get_session(now)
    status = session.status

    if status == SessionStatus.PREMARKET:
        if not settings.scheduler_news_premarket:
            return False, "Premarket news pull is disabled."
        if _ran_on_eastern_date(last_run, session.session_date):
            return False, "Premarket news pull already completed today."
        return True, "Premarket morning news pull."

    if status in (SessionStatus.REGULAR, SessionStatus.EARLY_CLOSE):
        if session.open_at is None or session.close_at is None:
            return False, "Session bounds unavailable."
        pulls = max(1, settings.scheduler_news_intraday_pulls)
        interval = (session.close_at - session.open_at).total_seconds() / pulls
        if last_run is None:
            return True, "First intraday news pull."
        elapsed = (now - last_run).total_seconds()
        if elapsed >= interval:
            return True, (
                f"Intraday news pull ({pulls} pulls spread every {int(interval)}s "
                "across the session)."
            )
        return False, f"Next intraday news pull in {int(max(0, interval - elapsed))}s."

    return False, f"Market is {status.value}; news pulls are paused."


def _cadence_elapsed(now: datetime, last: datetime | None, cadence: int) -> bool:
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() >= cadence


def _job_cadences(settings: Settings) -> dict[str, int]:
    """Map each scheduled job to its own cadence in seconds.

    Both ``run_forever`` and the ``"all"`` fan-out path use this so low-frequency
    jobs (news, SEC, universe, reviews, learning) only fire on their own schedule
    instead of running every loop alongside the high-frequency market-data job.
    """
    market = settings.scheduler_market_data_seconds
    return {
        "market_data": market,
        "features": market,
        "production_scanners": market,
        "regime": settings.scheduler_regime_seconds,
        "provider_health": settings.provider_health_max_age_seconds,
        "catalysts": settings.scheduler_catalyst_seconds,
        "news_screener": settings.scheduler_news_screener_seconds,
        "missing_candle_repair": settings.scheduler_news_seconds,
        "fill_reconciliation": settings.scheduler_fill_reconciliation_seconds,
        "trade_monitor": settings.scheduler_trade_monitor_seconds,
        "news": settings.scheduler_news_seconds,
        "sec": settings.scheduler_sec_seconds,
        "universe": settings.scheduler_sec_seconds,
        "reviews": settings.scheduler_review_seconds,
        "learning": settings.scheduler_review_seconds,
        "live_readiness": settings.scheduler_review_seconds,
    }
