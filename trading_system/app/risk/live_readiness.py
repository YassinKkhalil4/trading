from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import (
    DataQualityStatus,
    EnvironmentMode,
    ProviderHealthStatus,
    SessionStatus,
    StrategyStatus,
)
from trading_system.app.data.market_calendar import get_session
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict


LIVE_READINESS_VERSION = "live_readiness_v1"


@dataclass(frozen=True)
class LiveReadinessResult:
    overall_status: str
    live_allowed: bool
    report_id: str
    blockers: int
    warnings: int
    reason: str
    version: str = LIVE_READINESS_VERSION


@dataclass(frozen=True)
class LiveReadinessGateDetail:
    gate_name: str
    passed: bool
    blocking_reason: str
    checked_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "passed": self.passed,
            "blocking_reason": self.blocking_reason,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True)
class LiveReadinessDetailResult:
    overall_status: str
    checked_at: datetime
    gates: list[LiveReadinessGateDetail]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "checked_at": self.checked_at.isoformat(),
            "gates": [gate.to_dict() for gate in self.gates],
        }


class LiveReadinessService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def generate_report(self, *, actor: str = "system") -> LiveReadinessResult:
        now = datetime.now(UTC)
        checks = [
            self._check(
                "environment_mode_explicit_live",
                self.settings.environment_mode == EnvironmentMode.LIVE,
                "BLOCKER",
                f"ENVIRONMENT_MODE is {self.settings.environment_mode.value}; live readiness requires explicit live review mode.",
                {"environment_mode": self.settings.environment_mode.value},
            ),
            self._check(
                "live_order_path_enabled",
                self.settings.live_order_path_enabled,
                "BLOCKER",
                "Live order path must be explicitly enabled by configuration before live trading.",
                {"live_order_path_enabled": self.settings.live_order_path_enabled},
            ),
            self._check(
                "live_credentials_explicitly_enabled",
                self.settings.allow_live_trading
                and bool(self.settings.alpaca_live_api_key)
                and bool(self.settings.alpaca_live_secret_key)
                and self.settings.confirm_live_trading == "I_UNDERSTAND_RISK",
                "BLOCKER",
                "Live credentials and explicit human confirmation are required.",
                {
                    "allow_live_trading": self.settings.allow_live_trading,
                    "live_key_present": bool(self.settings.alpaca_live_api_key),
                    "live_secret_present": bool(self.settings.alpaca_live_secret_key),
                    "confirmation_ok": self.settings.confirm_live_trading == "I_UNDERSTAND_RISK",
                },
            ),
            self._check(
                "admin_session_secret_configured",
                bool(self.settings.admin_session_secret)
                and self.settings.admin_session_secret != "change-me",
                "BLOCKER",
                "Admin JWT/session signing secret must be configured from a non-default secret.",
                {"default_secret_in_use": self.settings.admin_session_secret == "change-me"},
            ),
            self._check(
                "active_human_live_approval",
                self.repository.active_live_trading_approval() is not None,
                "BLOCKER",
                "A current human live-trading approval record is required.",
                {"active_approval": self._active_approval_payload()},
            ),
            self._check(
                "no_active_kill_switches",
                self._count_active_kill_switches() == 0,
                "BLOCKER",
                "No active kill-switch events.",
                {"active_kill_switches": self._count_active_kill_switches()},
            ),
            self._check(
                "alpaca_market_data_healthy",
                self._provider_health_ok("alpaca_market_data"),
                "BLOCKER",
                "Alpaca market-data provider health must be HEALTHY and fresh.",
                {
                    "latest_provider_health": self._latest_provider_health_payload("alpaca_market_data"),
                    "max_age_seconds": self.settings.provider_health_max_age_seconds,
                },
            ),
            self._check(
                "alpaca_live_broker_healthy",
                self._provider_health_ok("alpaca_live"),
                "BLOCKER",
                "Alpaca live broker provider health must be HEALTHY and fresh before live order submission.",
                {
                    "latest_provider_health": self._latest_provider_health_payload("alpaca_live"),
                    "max_age_seconds": self.settings.provider_health_max_age_seconds,
                },
            ),
            self._check(
                "live_account_snapshot_usable",
                self._live_account_snapshot_ok(),
                "BLOCKER",
                "A fresh Alpaca live account snapshot with positive equity and buying power is required.",
                {
                    "latest_live_account_snapshot": self._latest_live_account_payload(),
                    "max_age_seconds": self.settings.provider_health_max_age_seconds,
                },
            ),
            self._check(
                "broker_reconciliation_clean",
                self._latest_broker_sync_ok(),
                "BLOCKER",
                "Latest live broker/internal reconciliation must be successful and mismatch-free.",
                {"latest_broker_sync": self._latest_row(models.BrokerSyncLog)},
            ),
            self._check(
                "approved_live_strategy_exists",
                self._approved_strategy_count() > 0,
                "BLOCKER",
                "At least one strategy must be approved small/full size before live readiness.",
                {"approved_strategy_count": self._approved_strategy_count()},
            ),
            self._check(
                "idempotency_keys_unique",
                not any(self._duplicate_idempotency_keys().values()),
                "BLOCKER",
                "Signal and order idempotency keys must be unique.",
                {"duplicates": self._duplicate_idempotency_keys()},
            ),
            self._check(
                "recent_market_data_present",
                self._recent_market_data_exists(now),
                "BLOCKER",
                "Recent Alpaca market-data stream events or clean candles are required.",
                {"lookback_minutes": 30, "provider": "alpaca_market_data"},
            ),
            self._check(
                "paper_execution_evidence",
                self._paper_evidence_ok(),
                "WARNING",
                "Paper mode should have risk checks, orders, fills, and broker sync history before live review.",
                self._paper_evidence_counts(),
            ),
        ]

        blockers = sum(1 for check in checks if not check["passed"] and check["severity"] == "BLOCKER")
        warnings = sum(1 for check in checks if not check["passed"] and check["severity"] == "WARNING")
        live_allowed = blockers == 0
        overall_status = "BLOCKED" if blockers else ("PASSED_WITH_WARNINGS" if warnings else "PASSED")
        reason = (
            "Live trading remains blocked; one or more required live-readiness gates failed."
            if blockers
            else "All live-readiness blockers passed. Live order endpoints still require per-order gates."
        )
        report = self.repository.store_live_readiness_report(
            overall_status=overall_status,
            live_allowed=live_allowed,
            reason=reason,
            checks=checks,
            actor=actor,
            source_timestamp=now,
        )
        return LiveReadinessResult(
            overall_status=overall_status,
            live_allowed=live_allowed,
            report_id=report.id,
            blockers=blockers,
            warnings=warnings,
            reason=reason,
        )

    def get_detail_report(self, *, checked_at: datetime | None = None) -> LiveReadinessDetailResult:
        reference = checked_at or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)

        gates = [
            self._detail_gate(
                "environment_mode",
                self.settings.environment_mode == EnvironmentMode.LIVE,
                (
                    f"ENVIRONMENT_MODE is {self.settings.environment_mode.value}; "
                    "live readiness requires explicit live review mode."
                ),
                reference,
            ),
            self._detail_gate(
                "live_config_present",
                self.settings.allow_live_trading,
                "Live trading must be explicitly enabled by configuration (allow_live_trading).",
                reference,
            ),
            self._detail_gate(
                "live_confirmation_phrase",
                self.settings.confirm_live_trading == "I_UNDERSTAND_RISK",
                "Live trading requires explicit human confirmation phrase I_UNDERSTAND_RISK.",
                reference,
            ),
            self._detail_gate(
                "live_keys_present",
                bool(self.settings.alpaca_live_api_key) and bool(self.settings.alpaca_live_secret_key),
                "Alpaca live API key and secret key must be configured.",
                reference,
            ),
            self._detail_gate(
                "admin_session_secret_not_default",
                bool(self.settings.admin_session_secret)
                and self.settings.admin_session_secret != "change-me",
                "Admin JWT/session signing secret must be configured from a non-default secret.",
                reference,
            ),
            self._detail_gate(
                "provider_health_fresh",
                self._provider_health_ok_at("alpaca_live", reference),
                "Alpaca live broker provider health must be HEALTHY and fresh.",
                reference,
            ),
            self._detail_gate(
                "alpaca_market_data_fresh",
                self._recent_market_data_exists(reference),
                "Recent Alpaca market-data stream events or clean candles are required.",
                reference,
            ),
            self._detail_gate(
                "broker_reconciliation_clean",
                self._latest_broker_sync_ok(),
                "Latest live broker/internal reconciliation must be successful and mismatch-free.",
                reference,
            ),
            self._detail_gate(
                "no_active_kill_switch",
                self._count_active_kill_switches() == 0,
                "No active kill-switch events.",
                reference,
            ),
            self._detail_gate(
                "approved_strategy",
                self._approved_strategy_count() > 0,
                "At least one strategy must be approved small/full size before live readiness.",
                reference,
            ),
            self._detail_gate(
                "human_live_approval_active",
                self.repository.active_live_trading_approval() is not None,
                "A current human live-trading approval record is required.",
                reference,
            ),
            self._data_quality_gate(reference),
            self._market_session_gate(reference),
        ]
        overall_status = "PASSED" if all(gate.passed for gate in gates) else "BLOCKED"
        return LiveReadinessDetailResult(
            overall_status=overall_status,
            checked_at=reference,
            gates=gates,
        )

    def _detail_gate(
        self,
        gate_name: str,
        passed: bool,
        blocking_reason: str,
        checked_at: datetime,
    ) -> LiveReadinessGateDetail:
        return LiveReadinessGateDetail(
            gate_name=gate_name,
            passed=passed,
            blocking_reason="" if passed else blocking_reason,
            checked_at=checked_at,
        )

    def _data_quality_gate(self, checked_at: datetime) -> LiveReadinessGateDetail:
        passed = self._data_quality_valid(checked_at)
        return self._detail_gate(
            "data_quality_valid",
            passed,
            "Recent Alpaca clean candles must have VALID data quality status.",
            checked_at,
        )

    def _market_session_gate(self, checked_at: datetime) -> LiveReadinessGateDetail:
        session = get_session(checked_at)
        passed = self._market_session_valid(checked_at)
        blocking_reason = session.reason if not passed else ""
        return self._detail_gate("market_session_valid", passed, blocking_reason, checked_at)

    def _data_quality_valid(self, checked_at: datetime) -> bool:
        cutoff = checked_at - timedelta(minutes=30)
        valid_count = self.repository.session.scalar(
            select(func.count())
            .select_from(models.CleanMarketData)
            .where(
                models.CleanMarketData.provider == "alpaca_market_data",
                models.CleanMarketData.source_timestamp >= cutoff,
                models.CleanMarketData.data_quality_status == DataQualityStatus.VALID.value,
            )
        )
        invalid_count = self.repository.session.scalar(
            select(func.count())
            .select_from(models.CleanMarketData)
            .where(
                models.CleanMarketData.provider == "alpaca_market_data",
                models.CleanMarketData.source_timestamp >= cutoff,
                models.CleanMarketData.data_quality_status != DataQualityStatus.VALID.value,
            )
        )
        return bool((valid_count or 0) > 0 and (invalid_count or 0) == 0)

    def _market_session_valid(self, checked_at: datetime) -> bool:
        session = get_session(checked_at)
        return session.status in {
            SessionStatus.REGULAR,
            SessionStatus.EARLY_CLOSE,
            SessionStatus.PREMARKET,
            SessionStatus.AFTER_HOURS,
        }

    def _provider_health_ok_at(self, provider_name: str, checked_at: datetime) -> bool:
        row = self.repository.latest_provider_health_for(provider_name)
        return bool(
            row
            and row.status == ProviderHealthStatus.HEALTHY.value
            and _timestamp_fresh_at(
                row.source_timestamp,
                reference=checked_at,
                max_age_seconds=self.settings.provider_health_max_age_seconds,
            )
        )

    def _check(
        self,
        check_name: str,
        passed: bool,
        severity: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "check_name": check_name,
            "passed": passed,
            "severity": severity,
            "reason": reason,
            "payload": payload or {},
        }

    def _count_active_kill_switches(self) -> int:
        return self.repository.active_kill_switch_count()

    def _approved_strategy_count(self) -> int:
        return int(
            self.repository.session.scalar(
                select(func.count())
                .select_from(models.StrategyRegistry)
                .where(
                    models.StrategyRegistry.status.in_(
                        [
                            StrategyStatus.APPROVED_SMALL_SIZE.value,
                            StrategyStatus.APPROVED_FULL_SIZE.value,
                        ]
                    )
                )
            )
            or 0
        )

    def _latest_broker_sync_ok(self) -> bool:
        latest = self.repository.latest_broker_sync(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        return bool(latest and latest.success and not latest.mismatch_detected)

    def _provider_health_ok(self, provider_name: str) -> bool:
        row = self.repository.latest_provider_health_for(provider_name)
        return bool(
            row
            and row.status == ProviderHealthStatus.HEALTHY.value
            and _timestamp_fresh(row.source_timestamp, max_age_seconds=self.settings.provider_health_max_age_seconds)
        )

    def _latest_provider_health_payload(self, provider_name: str) -> dict[str, Any] | None:
        row = self.repository.latest_provider_health_for(provider_name)
        return model_to_dict(row) if row else None

    def _live_account_snapshot_ok(self) -> bool:
        row = self.repository.latest_broker_account_snapshot(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        return bool(
            row
            and _timestamp_fresh(row.source_timestamp, max_age_seconds=self.settings.provider_health_max_age_seconds)
            and row.equity is not None
            and row.equity > 0
            and row.buying_power is not None
            and row.buying_power > 0
            and (not row.status or row.status.upper() == "ACTIVE")
        )

    def _latest_live_account_payload(self) -> dict[str, Any] | None:
        row = self.repository.latest_broker_account_snapshot(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        return model_to_dict(row) if row else None

    def _active_approval_payload(self) -> dict[str, Any] | None:
        row = self.repository.active_live_trading_approval()
        return model_to_dict(row) if row else None

    def _latest_row(self, model: type) -> dict[str, Any] | None:
        latest = self.repository.session.scalar(select(model).order_by(desc(model.created_at)).limit(1))
        return model_to_dict(latest) if latest else None

    def _duplicate_idempotency_keys(self) -> dict[str, list[str]]:
        duplicate_signals = self.repository.session.execute(
            select(models.Signal.idempotency_key)
            .group_by(models.Signal.idempotency_key)
            .having(func.count() > 1)
        ).scalars()
        duplicate_orders = self.repository.session.execute(
            select(models.Order.idempotency_key)
            .group_by(models.Order.idempotency_key)
            .having(func.count() > 1)
        ).scalars()
        return {"signals": list(duplicate_signals), "orders": list(duplicate_orders)}

    def _recent_market_data_exists(self, now: datetime) -> bool:
        cutoff = now - timedelta(minutes=30)
        stream_count = self.repository.session.scalar(
            select(func.count())
            .select_from(models.MarketDataStreamEvent)
            .where(
                models.MarketDataStreamEvent.provider == "alpaca_market_data",
                models.MarketDataStreamEvent.source_timestamp >= cutoff,
            )
        )
        candle_count = self.repository.session.scalar(
            select(func.count())
            .select_from(models.CleanMarketData)
            .where(
                models.CleanMarketData.provider == "alpaca_market_data",
                models.CleanMarketData.source_timestamp >= cutoff,
            )
        )
        return bool((stream_count or 0) + (candle_count or 0))

    def _paper_evidence_counts(self) -> dict[str, int]:
        return {
            "risk_checks": _count(self.repository, models.RiskCheck),
            "orders": _count(self.repository, models.Order),
            "fills": _count(self.repository, models.Fill),
            "broker_sync_logs": _count(self.repository, models.BrokerSyncLog),
        }

    def _paper_evidence_ok(self) -> bool:
        counts = self._paper_evidence_counts()
        return counts["risk_checks"] > 0 and counts["orders"] > 0 and counts["broker_sync_logs"] > 0


def _count(repository: TradingRepository, model: type) -> int:
    return int(repository.session.scalar(select(func.count()).select_from(model)) or 0)


def _timestamp_fresh_at(
    timestamp: datetime | None,
    *,
    reference: datetime,
    max_age_seconds: int,
) -> bool:
    if not timestamp:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return timestamp >= reference - timedelta(seconds=max_age_seconds)


def _timestamp_fresh(timestamp: datetime | None, *, max_age_seconds: int) -> bool:
    return _timestamp_fresh_at(
        timestamp,
        reference=datetime.now(UTC),
        max_age_seconds=max_age_seconds,
    )
