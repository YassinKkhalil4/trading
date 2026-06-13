from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import EnvironmentMode, ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict


LIVE_GATE_VERSION = "live_gates_v1"


@dataclass(frozen=True)
class LiveGateDecision:
    allowed: bool
    reason: str
    blockers: list[str]
    payload: dict[str, Any]
    version: str = LIVE_GATE_VERSION


class LiveGateService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def evaluate(self, *, strategy_id: str | None = None, signal_id: str | None = None) -> LiveGateDecision:
        return self._evaluate(
            strategy_id=strategy_id,
            signal_id=signal_id,
            action="live_order",
            require_strategy=True,
        )

    def evaluate_operational_action(self, *, action: str) -> LiveGateDecision:
        return self._evaluate(
            strategy_id=None,
            signal_id=None,
            action=action,
            require_strategy=False,
        )

    def _evaluate(
        self,
        *,
        strategy_id: str | None,
        signal_id: str | None,
        action: str,
        require_strategy: bool,
    ) -> LiveGateDecision:
        checks: dict[str, bool] = {
            "environment_mode_live": self.settings.environment_mode == EnvironmentMode.LIVE,
            "allow_live_trading": self.settings.allow_live_trading,
            "confirm_live_trading": self.settings.confirm_live_trading == "I_UNDERSTAND_RISK",
            "live_order_path_enabled": self.settings.live_order_path_enabled,
            "live_keys_present": bool(self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key),
            "active_human_approval": self.repository.active_live_trading_approval() is not None,
            "no_active_kill_switch": self.repository.active_kill_switch_count() == 0,
            "latest_readiness_passed": self._latest_readiness_passed(),
            "alpaca_market_data_healthy": self._provider_healthy("alpaca_market_data"),
            "alpaca_live_healthy": self._provider_healthy("alpaca_live"),
            "live_account_snapshot_usable": self._live_account_snapshot_usable(),
            "live_reconciliation_clean": self._live_reconciliation_clean(),
        }
        if require_strategy:
            checks["strategy_approved"] = self._strategy_approved(strategy_id)
        blockers = [name for name, passed in checks.items() if not passed]
        payload = {
            "checks": checks,
            "action": action,
            "strategy_id": strategy_id,
            "signal_id": signal_id,
            "latest_readiness_report": self._latest_readiness_payload(),
            "latest_live_broker_sync": self._latest_live_broker_sync_payload(),
            "latest_live_account_snapshot": self._latest_live_account_payload(),
        }
        if blockers:
            return LiveGateDecision(
                allowed=False,
                reason="Live order blocked by required gates: " + ", ".join(blockers),
                blockers=blockers,
                payload=payload,
            )
        return LiveGateDecision(
            allowed=True,
            reason="All live order gates passed.",
            blockers=[],
            payload=payload,
        )

    def _latest_readiness_passed(self) -> bool:
        row = self.repository.latest_live_readiness_report()
        if not row:
            return False
        live_allowed = row.live_allowed if hasattr(row, "live_allowed") else bool(row.success)
        if not live_allowed or not row.source_timestamp:
            return False
        ts = row.source_timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts >= datetime.now(UTC) - timedelta(minutes=self.settings.live_readiness_max_age_minutes)

    def _latest_readiness_payload(self) -> dict[str, Any] | None:
        row = self.repository.latest_live_readiness_report()
        return model_to_dict(row) if row else None

    def _provider_healthy(self, provider_name: str) -> bool:
        row = self.repository.latest_provider_health_for(provider_name)
        return bool(
            row
            and row.status == ProviderHealthStatus.HEALTHY.value
            and self._timestamp_fresh(row.source_timestamp, max_age_seconds=self.settings.provider_health_max_age_seconds)
        )

    def _live_reconciliation_clean(self) -> bool:
        row = self.repository.latest_broker_sync(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        if not row:
            return False
        mismatch_detected = (
            row.mismatch_detected
            if hasattr(row, "mismatch_detected")
            else isinstance(row.payload, dict) and row.payload.get("mismatch_detected")
        )
        return bool(row.success and not mismatch_detected)

    def _live_account_snapshot_usable(self) -> bool:
        row = self.repository.latest_broker_account_snapshot(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        return bool(
            row
            and self._timestamp_fresh(row.source_timestamp, max_age_seconds=self.settings.provider_health_max_age_seconds)
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

    def _latest_live_broker_sync_payload(self) -> dict[str, Any] | None:
        row = self.repository.latest_broker_sync(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
        )
        return model_to_dict(row) if row else None

    def _strategy_approved(self, strategy_id: str | None) -> bool:
        if not strategy_id:
            return False
        row = self.repository.session.scalar(
            select(models.StrategyRegistry)
            .where(models.StrategyRegistry.strategy_id == strategy_id)
            .order_by(models.StrategyRegistry.created_at.desc())
            .limit(1)
        )
        return bool(
            row
            and row.status
            in {
                StrategyStatus.APPROVED_SMALL_SIZE.value,
                StrategyStatus.APPROVED_FULL_SIZE.value,
            }
        )

    @staticmethod
    def _timestamp_fresh(timestamp: datetime | None, *, max_age_seconds: int) -> bool:
        if not timestamp:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp >= datetime.now(UTC) - timedelta(seconds=max_age_seconds)
