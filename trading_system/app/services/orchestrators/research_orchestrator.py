from __future__ import annotations

from trading_system.app.services.runtime_support import *  # noqa: F403,F401


class ResearchOrchestrator:
    """Research, backtesting, readiness, and dashboard orchestration."""

    def __init__(
        self,
        repository: TradingRepository,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.strategy_registry = StrategyRegistryService()
        self.cooldowns = StrategyCooldownBook()

    def bootstrap(self) -> dict[str, int]:
        if self.settings.auto_create_schema_enabled:
            self.repository.create_schema()
        self.repository.seed_defaults()
        AuthService(self.repository, self.settings).bootstrap_configured_admin()
        return self.repository.counts()

    def generate_live_readiness_report(self, *, actor: str = "system") -> LiveReadinessResult:
        self.bootstrap()
        return LiveReadinessService(self.repository, self.settings).generate_report(actor=actor)

    def run_backtest(
        self,
        *,
        strategy_id: str = "VWAP_RECLAIM",
        symbol: str = "SPY",
        provider: str = "alpaca_market_data",
    ) -> dict[str, Any]:
        self.bootstrap()
        frame = self.repository.clean_candles_df(
            symbol,
            provider=provider,
            limit=2_000,
            valid_only=True,
        )
        if frame.empty:
            frame = self.repository.clean_candles_df(
                symbol,
                provider="yahoo_chart",
                limit=2_000,
                valid_only=True,
            )
            provider = "yahoo_chart"
        assumptions = BacktestAssumptions()
        if len(frame) < 30:
            metrics = {
                "error": "At least 30 clean candles are required for a VWAP reclaim backtest."
            }
            report = self.repository.store_backtest_report(
                strategy_id=strategy_id,
                strategy_version="v1",
                universe_name=f"{symbol}:{provider}",
                assumptions=assumptions.__dict__,
                metrics=metrics,
                report_uri=None,
                survivorship_bias_warning=SURVIVORSHIP_BIAS_WARNING,
                reason="Backtest rejected due to insufficient clean candles.",
            )
            return {"report": model_to_dict(report), "metrics": metrics}
        relative_volume = frame["volume"] / frame["volume"].rolling(20, min_periods=1).mean()
        try:
            result = run_vwap_reclaim_backtest(
                frame,
                relative_volume=relative_volume,
                assumptions=assumptions,
            )
            metrics = result["stats"]
            reason = "VectorBT VWAP reclaim backtest completed."
        except Exception as exc:
            metrics = {"error": str(exc)}
            reason = "Backtest failed; error was persisted for review."
        report = self.repository.store_backtest_report(
            strategy_id=strategy_id,
            strategy_version="v1",
            universe_name=f"{symbol}:{provider}",
            assumptions=assumptions.__dict__,
            metrics=metrics,
            report_uri=None,
            survivorship_bias_warning=SURVIVORSHIP_BIAS_WARNING,
            reason=reason,
        )
        return {"report": model_to_dict(report), "metrics": metrics}

    def system_snapshot(self) -> dict[str, Any]:
        self.bootstrap()
        return {
            "counts": self.repository.counts(),
            "active_symbols": self.repository.active_symbols(),
            "clean_candles": self.repository.latest_clean_candles(200),
            "features": self.repository.latest_features(100),
            "daily_features": self.repository.latest_daily_features(100),
            "regime_snapshots": self.repository.latest_regime_snapshots(100),
            "scanner_results": self.repository.latest_scanner_results(100),
            "signals": self.repository.latest_signals(100),
            "risk_checks": self.repository.latest_risk_checks(100),
            "broker_account_snapshots": self.repository.latest_broker_account_snapshots(100),
            "orders": self.repository.latest_orders(100),
            "positions": self.repository.latest_positions(100),
            "exposure_snapshots": self.repository.latest_exposure_snapshots(100),
            "execution_errors": self.repository.latest_execution_errors(100),
            "journal": self.repository.latest_journal(100),
            "decisions": self.repository.latest_decisions(200),
            "audit_logs": self.repository.latest_audit_logs(200),
            "api_calls": self.repository.latest_api_calls(100),
            "data_quality_errors": self.repository.latest_data_quality_errors(100),
            "provider_health": self.repository.latest_provider_health(100),
            "provider_rate_limits": self.repository.latest_provider_rate_limits(100),
            "worker_heartbeats": self.repository.latest_worker_heartbeats(100),
            "stream_events": self.repository.latest_stream_events(100),
            "scheduler_runs": self.repository.latest_scheduler_runs(100),
            "clean_news": self.repository.latest_clean_news(100),
            "filings": self.repository.latest_filings(100),
            "events": self.repository.latest_events(100),
            "catalysts": self.repository.latest_catalysts(100),
            "fills": self.repository.latest_fills(100),
            "broker_sync_logs": self.repository.latest_broker_sync_logs(100),
            "weekly_reviews": self.repository.latest_weekly_reviews(20),
            "strategy_recommendations": self.repository.latest_strategy_recommendations(100),
            "backtest_reports": self.repository.latest_backtest_reports(50),
            "live_readiness_reports": self.repository.latest_live_readiness_reports(20),
            "kill_switches": self.repository.latest_kill_switches(100),
            "missing_candle_gaps": self.repository.latest_missing_candle_gaps(100),
            "opportunity_scores": self.repository.latest_opportunity_scores(100),
            "alpha_rejections": self.repository.latest_alpha_rejections(100),
            "expectancy_snapshots": self.repository.latest_expectancy_snapshots(100),
            "sector_strength": self.repository.latest_sector_strength(100),
            "symbol_relative_strength": self.repository.latest_symbol_relative_strength(100),
            "point_in_time_universe": self.repository.latest_point_in_time_universe_memberships(
                100
            ),
            "short_interest": self.repository.latest_short_interest_snapshots(100),
            "options_intelligence": self.repository.latest_options_intelligence_snapshots(100),
            "providers": self.repository.list_rows(models.ProviderCapability, 100),
            "strategies": self.repository.list_rows(models.StrategyRegistry, 100),
        }
