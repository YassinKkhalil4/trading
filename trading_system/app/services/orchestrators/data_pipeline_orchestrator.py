from __future__ import annotations

from trading_system.app.services.runtime_support import *  # noqa: F403,F401


class DataPipelineOrchestrator:
    """Data ingestion, feature generation, catalyst, universe, and scanner orchestration."""

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

    def collect_symbol(self, symbol: str) -> YahooChartResult:
        collector = YahooChartCollector(self.repository)
        return collector.collect(symbol)

    def collect_symbol_primary(
        self,
        symbol: str,
        require_primary: bool = False,
    ) -> AlpacaBarsResult | YahooChartResult:
        collector = AlpacaBarsCollector(self.repository, self.settings)
        result = collector.collect(symbol)
        if result.success or require_primary or self.settings.environment_mode != EnvironmentMode.RESEARCH:
            return result
        return self.collect_symbol(symbol)

    def collect_active_symbols(self) -> list[YahooChartResult]:
        self.bootstrap()
        return [self.collect_symbol(symbol) for symbol in self.repository.active_symbols()]

    def run_watchlist_scan(self, *, collect_first: bool = True) -> list[ScanCycleResult]:
        self.bootstrap()
        results: list[ScanCycleResult] = []
        for symbol in self.repository.active_symbols():
            collected = self.collect_symbol(symbol) if collect_first else None
            results.append(self.run_vwap_scan(symbol, collected=collected))
        return results

    def run_vwap_scan(
        self,
        symbol: str,
        *,
        collected: AlpacaBarsResult | YahooChartResult | None = None,
    ) -> ScanCycleResult:
        normalized = symbol.strip().upper()
        require_primary = self.settings.environment_mode == EnvironmentMode.LIVE
        if require_primary and collected is None:
            collected = self.collect_symbol_primary(normalized, require_primary=True)
        if require_primary and isinstance(collected, AlpacaBarsResult) and not collected.success:
            reason = "Primary market data unavailable; live trading scan aborted."
            self._activate_primary_data_kill_switch(
                symbol=normalized,
                reason=reason,
                payload={"collector_reason": collected.reason},
            )
            self.repository.store_decision_log(
                decision_type=DecisionType.SCANNER,
                outcome=DecisionOutcome.REJECTED,
                entity_type="symbol",
                entity_id=normalized,
                strategy_id="VWAP_RECLAIM",
                rule_version="vwap_reclaim_scanner_v1",
                reason=reason,
                payload={"collector_reason": collected.reason},
            )
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=None,
                signal_id=None,
                thesis_id=None,
                reason=reason,
            )

        frame = self.repository.clean_candles_df(normalized, limit=500)
        if len(frame) < 2:
            self.repository.store_decision_log(
                decision_type=DecisionType.SCANNER,
                outcome=DecisionOutcome.REJECTED,
                entity_type="symbol",
                entity_id=normalized,
                strategy_id="VWAP_RECLAIM",
                rule_version="vwap_reclaim_scanner_v1",
                reason="Not enough valid clean candles to scan.",
                payload={"rows": len(frame)},
            )
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=None,
                signal_id=None,
                thesis_id=None,
                reason="Not enough valid clean candles to scan.",
            )

        snapshot, feature_payload = self._build_vwap_snapshot(normalized, frame)
        self.repository.store_intraday_features(
            symbol=normalized,
            source_timestamp=snapshot.timestamp,
            feature_version=FEATURE_CALCULATION_VERSION,
            price=snapshot.price,
            vwap=snapshot.vwap,
            atr=feature_payload.get("atr"),
            relative_volume=snapshot.relative_volume,
            gap_pct=None,
            volume_spike_score=feature_payload.get("volume_spike_score"),
            liquidity_score=feature_payload.get("liquidity_score"),
            spread_score=feature_payload.get("spread_score"),
        )
        self.repository.store_feature_snapshot(
            symbol=normalized,
            source_timestamp=snapshot.timestamp,
            feature_version=FEATURE_CALCULATION_VERSION,
            snapshot=feature_payload,
        )

        scanner = VwapReclaimScanner(
            liquidity_gates=LiquidityGates(
                min_price=self.settings.min_price,
                min_average_volume=self.settings.min_average_volume,
                min_dollar_volume=self.settings.min_dollar_volume,
                max_spread_bps=self.settings.max_spread_bps,
            ),
            strategy_registry=self.strategy_registry,
            cooldowns=self.cooldowns,
        )
        decision = scanner.scan(snapshot)
        # Always persist a preflight payload (plus latest close/VWAP) alongside the
        # scanner result. This is additive to the existing payload shape and lets the
        # opportunity-ranking engine score every accepted scanner result, regardless of
        # whether the ranking-gated signal path is enabled.
        preflight = build_preflight_payload(
            self.repository,
            symbol=normalized,
            strategy_id="VWAP_RECLAIM",
            timeframe="1Min",
            latest_data_timestamp=snapshot.timestamp,
        )
        scanner_row = self.repository.store_scanner_result(
            decision,
            source_timestamp=snapshot.timestamp,
            payload={
                "snapshot": _snapshot_to_payload(snapshot),
                "features": feature_payload,
                "spread_note": feature_payload.get("spread_note"),
                "data_source": feature_payload.get("data_source"),
                "spread_is_proxy": feature_payload.get("spread_is_proxy"),
                "preflight": preflight,
                "latest_close": snapshot.price,
                "latest_vwap": snapshot.vwap,
            },
        )

        if not decision.accepted:
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=scanner_row.id,
                signal_id=None,
                thesis_id=None,
                reason=decision.reason,
            )

        if self.settings.enable_ranking_signal_path:
            return self._create_signal_via_ranking(
                normalized,
                scanner_row,
                snapshot,
                decision,
                collected,
            )

        strategy = StrategyRegistryService().get(decision.strategy_id)
        stop_loss = min(snapshot.vwap, snapshot.price * 0.995)
        signal = SignalEngine().create_vwap_reclaim_signal(
            scanner_decision=decision,
            source_timestamp=snapshot.timestamp,
            price=snapshot.price,
            stop_loss=stop_loss,
            strategy_version=strategy.version,
            target_1_rr=strategy.target_1_rr,
            target_2_rr=strategy.target_2_rr,
            alpha_features={
                "vwap_distance": (snapshot.price - snapshot.vwap) / snapshot.vwap if snapshot.vwap else 0.0,
                "relative_volume_5m": snapshot.relative_volume,
                "spy_correlation_30m": 0.0,
                "atr_ratio": (snapshot.price - stop_loss) / snapshot.price if snapshot.price else 0.0,
            },
        )
        signal_row = self.repository.store_signal(signal)
        self.repository.store_signal_version(
            signal_id=signal_row.id,
            version=signal.rule_version,
            change_reason="Initial signal generated from VWAP reclaim scan.",
            payload=_trade_signal_to_payload(signal),
            source_timestamp=snapshot.timestamp,
        )
        return ScanCycleResult(
            symbol=normalized,
            collected=collected,
            scanner_result_id=scanner_row.id,
            signal_id=signal_row.id,
            thesis_id=None,
            reason="Signal generated; deprecated trade thesis persistence has been removed.",
        )

    def _create_signal_via_ranking(
        self,
        normalized: str,
        scanner_row: models.ScannerResult,
        snapshot: VwapReclaimSnapshot,
        decision: Any,
        collected: AlpacaBarsResult | YahooChartResult | None,
    ) -> ScanCycleResult:
        """Route an accepted scanner result through the opportunity-ranking gate.

        A signal (and thesis) is only created when the ranking engine grades the
        candidate highly enough for the bridge to accept it. Otherwise the scan
        returns with the bridge's blocked reason and no signal.
        """
        bridge = ScannerSignalBridgeService(self.repository, self.settings)
        bridge_result = bridge.try_create_signal(scanner_row.id, now=snapshot.timestamp)
        if not bridge_result.created or bridge_result.signal_id is None:
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=scanner_row.id,
                signal_id=None,
                thesis_id=None,
                reason=bridge_result.blocked_reason or "Ranking gate did not produce a signal.",
            )

        return ScanCycleResult(
            symbol=normalized,
            collected=collected,
            scanner_result_id=scanner_row.id,
            signal_id=bridge_result.signal_id,
            thesis_id=None,
            reason="Ranked signal generated; deprecated trade thesis persistence has been removed.",
        )

    async def run_alpaca_market_data_stream(
        self,
        *,
        symbols: list[str] | None = None,
        channels: list[str] | None = None,
        max_messages: int | None = 25,
    ) -> AlpacaStreamRunResult:
        self.bootstrap()
        stream = AlpacaMarketDataStream(self.repository, self.settings)
        return await stream.run(symbols=symbols, channels=channels, max_messages=max_messages)

    def collect_news(self, symbols: list[str] | None = None) -> NewsCollectionResult:
        self.bootstrap()
        return AlphaVantageNewsCollector(self.repository, self.settings).collect(symbols)

    def collect_sec_filings(
        self,
        symbols: list[str] | None = None,
        *,
        max_filings_per_symbol: int = 10,
    ) -> SecCollectionResult:
        self.bootstrap()
        return SecEdgarCollector(self.repository, self.settings).collect(
            symbols,
            max_filings_per_symbol=max_filings_per_symbol,
        )

    def run_scheduled_job(
        self,
        job_name: str,
        *,
        symbols: list[str] | None = None,
        actor: str = "system",
    ) -> ScheduledJobResult:
        self.bootstrap()
        return ScheduledCollectorRunner(self.repository, self.settings).run_once(
            job_name,
            symbols=symbols,
            actor=actor,
        )

    def run_provider_health(self) -> ProviderHealthRunResult:
        self.bootstrap()
        result = ProviderHealthService(self.repository, self.settings).run_once()
        if self.settings.environment_mode == EnvironmentMode.LIVE:
            health = self.repository.latest_provider_health_for("alpaca_market_data")
            failure_streak = int(health.failure_streak or 0) if health else 0
            if (
                health
                and health.status != "HEALTHY"
                and failure_streak >= self.settings.primary_market_data_failure_kill_switch_streak
            ):
                self._activate_primary_data_kill_switch(
                    symbol=None,
                    reason="Primary Alpaca market data health is not healthy; live trading disabled until restored.",
                    payload={
                        "provider_name": health.provider_name,
                        "status": health.status,
                        "failure_streak": health.failure_streak,
                        "health_reason": health.reason,
                    },
                )
        return result

    def run_features(self, symbols: list[str] | None = None) -> FeatureRunResult:
        self.bootstrap()
        return ProductionFeatureEngine(self.repository).run_once(symbols)

    def run_market_regime(self) -> RegimeRunResult:
        self.bootstrap()
        return MarketRegimeService(self.repository).run_once()

    def run_catalysts(self, symbols: list[str] | None = None) -> CatalystRunResult:
        self.bootstrap()
        return CatalystEngine(self.repository).run_once(symbols)

    def run_production_scanners(
        self, symbols: list[str] | None = None
    ) -> ProductionScannerRunResult:
        self.bootstrap()
        return ProductionScannerEngine(self.repository).run_once(symbols)

    def run_trade_monitor(self) -> TradeMonitorRunResult:
        self.bootstrap()
        return TradeMonitorService(self.repository).run_once()

    def run_learning_review(self) -> LearningRunResult:
        self.bootstrap()
        return LearningRecommendationEngine(self.repository).run_weekly_review()

    def refresh_universe(self, symbols: list[str] | None = None) -> UniverseRefreshResult:
        self.bootstrap()
        return LiquidUniverseBuilder(self.repository, self.settings).refresh(symbols)

    def repair_missing_candles(self, symbols: list[str] | None = None) -> MissingCandleRepairResult:
        self.bootstrap()
        return MissingCandleRepairService(self.repository, self.settings).run_once(symbols)

    def activate_kill_switch(
        self,
        *,
        event_type: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> KillSwitchActionResult:
        self.bootstrap()
        return KillSwitchService(self.repository).activate(
            event_type=event_type,
            reason=reason,
            payload=payload,
            actor=actor,
        )

    def resolve_kill_switch(
        self,
        *,
        event_id: str,
        reason: str,
        actor: str = "system",
    ) -> KillSwitchActionResult:
        self.bootstrap()
        return KillSwitchService(self.repository).resolve(
            event_id=event_id, reason=reason, actor=actor
        )

    def _activate_primary_data_kill_switch(
        self,
        *,
        symbol: str | None,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.settings.environment_mode != EnvironmentMode.LIVE:
            return
        merged_payload = {"provider_name": "alpaca_market_data"}
        if symbol is not None:
            merged_payload["symbol"] = symbol
        if payload:
            merged_payload.update(payload)
        self.repository.activate_kill_switch(
            event_type="PRIMARY_MARKET_DATA_UNAVAILABLE",
            reason=reason,
            payload=merged_payload,
        )

    def _build_vwap_snapshot(
        self,
        symbol: str,
        frame: pd.DataFrame,
    ) -> tuple[VwapReclaimSnapshot, dict[str, Any]]:
        ordered = frame.sort_index()
        vwap = calculate_vwap(ordered)
        atr = calculate_atr(ordered)
        latest = ordered.iloc[-1]
        previous = ordered.iloc[-2]
        session_volume = float(ordered["volume"].sum())
        current_volume = float(latest["volume"])
        relative_volume = calculate_relative_volume(
            current_volume, max(1.0, float(ordered["volume"].tail(20).mean()))
        )
        dollar_volume = float((ordered["close"] * ordered["volume"]).sum())
        data_source = str(latest.get("provider") or "unknown")
        spread_is_proxy = data_source in {"yahoo_chart", "yfinance", "unknown"}
        spread_bps = calculate_spread_bps(float(latest["low"]), float(latest["high"]))
        volume_spike_score = calculate_volume_spike_score(relative_volume)
        liquidity_score = min(100.0, dollar_volume / max(1.0, self.settings.min_dollar_volume) * 50)
        spread_score = max(0.0, 100.0 - spread_bps)
        snapshot = VwapReclaimSnapshot(
            symbol=symbol,
            timestamp=ordered.index[-1].to_pydatetime(),
            price=float(latest["close"]),
            previous_price=float(previous["close"]),
            vwap=float(vwap.iloc[-1]),
            previous_vwap=float(vwap.iloc[-2]),
            relative_volume=relative_volume,
            average_volume=session_volume,
            dollar_volume=dollar_volume,
            spread_bps=spread_bps,
            market_regime=MarketRegime.CHOPPY,
            has_catalyst=False,
            strong_relative_strength=bool(float(latest["close"]) > float(vwap.iloc[-1])),
        )
        feature_payload = {
            "symbol": symbol,
            "source_timestamp": snapshot.timestamp.isoformat(),
            "feature_version": FEATURE_CALCULATION_VERSION,
            "price": snapshot.price,
            "vwap": snapshot.vwap,
            "atr": float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
            "relative_volume": relative_volume,
            "volume_spike_score": volume_spike_score,
            "liquidity_score": liquidity_score,
            "spread_score": spread_score,
            "spread_bps": spread_bps,
            "dollar_volume": dollar_volume,
            "session_volume_so_far": session_volume,
            "data_source": data_source,
            "spread_is_proxy": spread_is_proxy,
            "spread_note": (
                "Proxy from current candle high/low because Yahoo chart has no bid/ask quote."
                if spread_is_proxy
                else "Primary Alpaca bar range used; execution risk still requires live quote validation."
            ),
        }
        return snapshot, feature_payload
