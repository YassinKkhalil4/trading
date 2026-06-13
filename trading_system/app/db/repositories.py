from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_system.app.ai.thesis_engine import AIThesis
from trading_system.app.core.enums import (
    DataQualityStatus,
    DecisionOutcome,
    DecisionType,
    Direction,
    ExecutionEnvironment,
    LiveApprovalStatus,
    OrderStatus,
    ProviderHealthStatus,
    SignalStatus,
    StrategyApprovalStatus,
    TradeType,
)
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.seed import (
    DEFAULT_PROVIDER_CAPABILITIES,
    DEFAULT_STRATEGIES,
    DEFAULT_SYMBOLS,
)
from trading_system.app.execution.order_side import entry_side_from_direction, normalize_order_side
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.risk.risk_engine import RiskDecision
from trading_system.app.scanners.vwap_reclaim import ScannerDecision
from trading_system.app.signals.signal_engine import TradeSignal


def model_to_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name, None) for column in row.__table__.columns}


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return _now()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class TradingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_schema(self) -> None:
        Base.metadata.create_all(bind=self.session.get_bind())

    def seed_defaults(self) -> None:
        for payload in DEFAULT_PROVIDER_CAPABILITIES:
            existing = self.session.scalar(
                select(models.ProviderCapability).where(
                    models.ProviderCapability.provider_name == payload["provider_name"]
                )
            )
            if existing:
                for key, value in payload.items():
                    setattr(existing, key, value)
            else:
                self.session.add(models.ProviderCapability(**payload))

        for payload in DEFAULT_STRATEGIES:
            existing = self.session.scalar(
                select(models.StrategyRegistry).where(
                    models.StrategyRegistry.strategy_id == payload["strategy_id"],
                    models.StrategyRegistry.version == payload["version"],
                )
            )
            if existing:
                for key, value in payload.items():
                    if key in {"status", "paused_reason", "changed_reason"}:
                        continue
                    setattr(existing, key, value)
            else:
                self.session.add(models.StrategyRegistry(source_timestamp=_now(), **payload))

        for payload in DEFAULT_SYMBOLS:
            existing = self.session.scalar(
                select(models.SymbolUniverse).where(
                    models.SymbolUniverse.symbol == payload["symbol"]
                )
            )
            if existing:
                for key, value in payload.items():
                    setattr(existing, key, value)
            else:
                self.session.add(models.SymbolUniverse(source_timestamp=_now(), **payload))

        self.session.commit()

    def active_symbols(self) -> list[str]:
        rows = self.session.scalars(
            select(models.SymbolUniverse.symbol)
            .where(models.SymbolUniverse.is_active.is_(True))
            .order_by(models.SymbolUniverse.symbol)
        ).all()
        return list(rows)

    def add_or_activate_symbol(
        self,
        symbol: str,
        *,
        name: str | None = None,
        sector: str | None = None,
        reason: str = "Added from dashboard.",
    ) -> models.SymbolUniverse:
        normalized = symbol.strip().upper()
        existing = self.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == normalized)
        )
        if existing:
            existing.is_active = True
            existing.change_reason = reason
            if name:
                existing.name = name
            if sector:
                existing.sector = sector
            self.session.commit()
            return existing
        row = models.SymbolUniverse(
            symbol=normalized,
            name=name,
            sector=sector,
            asset_class="US_EQUITY",
            is_active=True,
            change_reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def deactivate_symbol(self, symbol: str, reason: str) -> models.SymbolUniverse | None:
        row = self.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol.upper())
        )
        if not row:
            return None
        row.is_active = False
        row.change_reason = reason
        self.session.commit()
        return row

    def set_symbol_tradability(
        self,
        symbol: str,
        *,
        is_tradable: bool,
        reason: str,
    ) -> models.SymbolUniverse | None:
        row = self.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol.upper())
        )
        if not row:
            return None
        row.is_tradable = is_tradable
        row.tradability_reason = reason
        row.change_reason = reason
        self.session.commit()
        return row

    def upsert_admin_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str,
        reason: str,
    ) -> models.AdminUser:
        row = self.session.scalar(
            select(models.AdminUser).where(models.AdminUser.username == username)
        )
        if not row:
            row = models.AdminUser(
                username=username,
                password_hash=password_hash,
                role=role,
                reason=reason,
                source_timestamp=_now(),
            )
            self.session.add(row)
        else:
            row.password_hash = password_hash
            row.role = role
            row.reason = reason
            row.is_active = True
        self.session.commit()
        return row

    def list_admin_users(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.AdminUser).order_by(desc(models.AdminUser.created_at)).limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "username": row.username,
                "role": row.role,
                "is_active": row.is_active,
                "failed_login_count": row.failed_login_count,
                "locked_until": row.locked_until,
                "last_login_at": row.last_login_at,
                "reason": row.reason,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "source_timestamp": row.source_timestamp,
            }
            for row in rows
        ]

    def set_admin_user_active(
        self, *, username: str, is_active: bool, reason: str
    ) -> models.AdminUser:
        row = self.admin_user_by_username(username)
        if not row:
            raise ValueError(f"Unknown admin user: {username}")
        row.is_active = is_active
        row.reason = reason
        self.session.commit()
        return row

    def set_admin_user_role(self, *, username: str, role: str, reason: str) -> models.AdminUser:
        row = self.admin_user_by_username(username)
        if not row:
            raise ValueError(f"Unknown admin user: {username}")
        row.role = role
        row.reason = reason
        self.session.commit()
        return row

    def clear_admin_user_lockout(self, *, username: str, reason: str) -> models.AdminUser:
        row = self.admin_user_by_username(username)
        if not row:
            raise ValueError(f"Unknown admin user: {username}")
        row.failed_login_count = 0
        row.locked_until = None
        row.reason = reason
        self.session.commit()
        return row

    def admin_user_by_username(self, username: str) -> models.AdminUser | None:
        return self.session.scalar(
            select(models.AdminUser).where(models.AdminUser.username == username)
        )

    def admin_user_by_id(self, user_id: str) -> models.AdminUser | None:
        return self.session.get(models.AdminUser, user_id)

    def store_admin_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        reason: str,
    ) -> models.AdminSession:
        row = models.AdminSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def admin_session_by_hash(self, token_hash: str) -> models.AdminSession | None:
        return self.session.scalar(
            select(models.AdminSession).where(
                models.AdminSession.token_hash == token_hash,
                models.AdminSession.revoked_at.is_(None),
                models.AdminSession.expires_at > _now(),
            )
        )

    def revoke_admin_session(self, token_hash: str, reason: str) -> bool:
        row = self.session.scalar(
            select(models.AdminSession).where(models.AdminSession.token_hash == token_hash)
        )
        if not row:
            return False
        row.revoked_at = _now()
        row.reason = reason
        self.session.commit()
        return True

    def revoke_admin_sessions_for_user(self, *, user_id: str, reason: str) -> int:
        rows = self.session.scalars(
            select(models.AdminSession).where(
                models.AdminSession.user_id == user_id,
                models.AdminSession.revoked_at.is_(None),
                models.AdminSession.expires_at > _now(),
            )
        ).all()
        ts = _now()
        for row in rows:
            row.revoked_at = ts
            row.reason = reason
        if rows:
            self.session.commit()
        return len(rows)

    def record_failed_login(self, username: str, *, locked_until: datetime | None = None) -> None:
        row = self.admin_user_by_username(username)
        if not row:
            return
        row.failed_login_count += 1
        row.locked_until = locked_until
        self.session.commit()

    def record_successful_login(self, user: models.AdminUser) -> None:
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = _now()
        self.session.commit()

    def log_api_call(
        self,
        *,
        provider: str,
        endpoint: str,
        status_code: int | None,
        success: bool,
        reason: str,
        duration_ms: float | None = None,
        request_hash: str | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.ApiCallLog:
        row = models.ApiCallLog(
            provider=provider,
            endpoint=endpoint,
            status_code=status_code,
            success=success,
            reason=reason,
            duration_ms=duration_ms,
            request_hash=request_hash,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_provider_health_snapshot(
        self,
        *,
        provider_name: str,
        status: str,
        reason: str,
        last_success_at: datetime | None = None,
        last_failure_at: datetime | None = None,
        failure_streak: int = 0,
        latency_ms: float | None = None,
        freshness_seconds: float | None = None,
        reliability_score: float = 0.0,
        payload: dict | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.ProviderHealthSnapshot:
        row = models.ProviderHealthSnapshot(
            provider_name=provider_name,
            status=status,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            failure_streak=failure_streak,
            latency_ms=latency_ms,
            freshness_seconds=freshness_seconds,
            reliability_score=reliability_score,
            reason=reason,
            payload=payload,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_provider_rate_limit_state(
        self,
        *,
        provider_name: str,
        endpoint: str | None,
        limit_remaining: int | None,
        reset_at: datetime | None,
        request_count: int,
        blocked_until: datetime | None,
        reason: str,
    ) -> models.ProviderRateLimitState:
        row = models.ProviderRateLimitState(
            provider_name=provider_name,
            endpoint=endpoint,
            limit_remaining=limit_remaining,
            reset_at=reset_at,
            request_count=request_count,
            blocked_until=blocked_until,
            reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_raw_candle(self, payload: dict) -> str:
        source_timestamp = _as_utc(payload["source_timestamp"])
        received_at = _as_utc(payload.get("received_at"))
        processed_at = _now()
        existing = self.session.scalar(
            select(models.RawMarketData).where(
                models.RawMarketData.provider == payload["provider"],
                models.RawMarketData.symbol == payload["symbol"].upper(),
                models.RawMarketData.timeframe == payload["timeframe"],
                models.RawMarketData.source_timestamp == source_timestamp,
            )
        )
        if existing:
            return existing.id

        row = models.RawMarketData(
            provider=payload["provider"],
            symbol=payload["symbol"].upper(),
            timeframe=payload["timeframe"],
            source_timestamp=source_timestamp,
            raw_payload=payload["raw_payload"],
            received_at=received_at,
            processed_at=processed_at,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(models.RawMarketData).where(
                    models.RawMarketData.provider == payload["provider"],
                    models.RawMarketData.symbol == payload["symbol"].upper(),
                    models.RawMarketData.timeframe == payload["timeframe"],
                    models.RawMarketData.source_timestamp == source_timestamp,
                )
            )
            if existing:
                return existing.id
            raise
        self._record_raw_ingestion_event(
            payload_type="raw_market_bars",
            provider=payload["provider"],
            symbol=payload["symbol"],
            raw_table=models.RawMarketData.__tablename__,
            raw_row_id=row.id,
            raw_payload=payload["raw_payload"],
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
        )
        self._archive_raw_payload(
            category="market_data",
            provider=payload["provider"],
            symbol=payload["symbol"],
            row_id=row.id,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
            payload=payload["raw_payload"],
        )
        return row.id

    def enqueue_raw_market_bar(self, payload: dict) -> str:
        return self.store_raw_candle(payload)

    def store_raw_trade_tick(
        self,
        *,
        provider: str,
        symbol: str,
        raw_payload: dict,
        source_timestamp: datetime,
        trade_id: str | None = None,
        price: float | None = None,
        size: float | None = None,
        exchange: str | None = None,
        conditions: list[str] | None = None,
        received_at: datetime | None = None,
    ) -> models.RawTradeTick:
        source_timestamp = _as_utc(source_timestamp)
        received_at = _as_utc(received_at)
        processed_at = _now()
        existing = None
        if trade_id:
            existing = self.session.scalar(
                select(models.RawTradeTick).where(
                    models.RawTradeTick.provider == provider,
                    models.RawTradeTick.symbol == symbol.upper(),
                    models.RawTradeTick.trade_id == trade_id,
                )
            )
        if existing:
            return existing
        row = models.RawTradeTick(
            provider=provider,
            symbol=symbol.upper(),
            trade_id=trade_id or "",
            price=price,
            size=size,
            exchange=exchange,
            conditions=conditions,
            raw_payload=raw_payload,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
        )
        self.session.add(row)
        self.session.commit()
        self._record_raw_ingestion_event(
            payload_type="raw_trade_ticks",
            provider=provider,
            symbol=symbol,
            raw_table=models.RawTradeTick.__tablename__,
            raw_row_id=row.id,
            raw_payload=raw_payload,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
        )
        self._archive_raw_payload(
            category="trade_ticks",
            provider=provider,
            symbol=symbol,
            row_id=row.id,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
            payload=raw_payload,
        )
        return row

    def enqueue_raw_trade_tick(self, **kwargs: Any) -> models.RawTradeTick:
        return self.store_raw_trade_tick(**kwargs)

    def store_clean_candle(self, payload: dict) -> str:
        payload = dict(payload)
        payload["symbol"] = payload["symbol"].upper()
        payload["source_timestamp"] = _as_utc(payload["source_timestamp"])
        existing = self.session.scalar(
            select(models.CleanMarketData).where(
                models.CleanMarketData.provider == payload["provider"],
                models.CleanMarketData.symbol == payload["symbol"],
                models.CleanMarketData.timeframe == payload["timeframe"],
                models.CleanMarketData.source_timestamp == payload["source_timestamp"],
            )
        )
        if existing:
            self.store_data_quality_error(
                provider=payload["provider"],
                symbol=payload["symbol"],
                timeframe=payload["timeframe"],
                data_quality_status=DataQualityStatus.SUSPICIOUS_PRICE.value,
                reason="Duplicate provider/symbol/timeframe candle received.",
                source_timestamp=payload["source_timestamp"],
                payload=payload,
            )
            return existing.id

        self._apply_candle_quality_blocks(payload)
        row = models.CleanMarketData(**payload)
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(models.CleanMarketData).where(
                    models.CleanMarketData.provider == payload["provider"],
                    models.CleanMarketData.symbol == payload["symbol"],
                    models.CleanMarketData.timeframe == payload["timeframe"],
                    models.CleanMarketData.source_timestamp == payload["source_timestamp"],
                )
            )
            if existing:
                return existing.id
            raise
        if row.data_quality_status != DataQualityStatus.VALID.value:
            self.store_data_quality_error(
                provider=row.provider,
                symbol=row.symbol,
                timeframe=row.timeframe,
                data_quality_status=row.data_quality_status,
                reason=row.quality_reason or "Clean candle failed repository quality checks.",
                source_timestamp=row.source_timestamp,
                payload=payload,
            )
        return row.id

    def _apply_candle_quality_blocks(self, payload: dict) -> None:
        if payload.get("data_quality_status") != DataQualityStatus.VALID.value:
            return
        previous = self.session.scalar(
            select(models.CleanMarketData)
            .where(
                models.CleanMarketData.provider == payload["provider"],
                models.CleanMarketData.symbol == payload["symbol"],
                models.CleanMarketData.timeframe == payload["timeframe"],
                models.CleanMarketData.source_timestamp < payload["source_timestamp"],
            )
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(1)
        )
        if not previous:
            return
        timeframe_seconds = _timeframe_seconds(payload["timeframe"])
        if timeframe_seconds:
            gap_seconds = (
                payload["source_timestamp"] - _as_utc(previous.source_timestamp)
            ).total_seconds()
            if gap_seconds > timeframe_seconds * 1.5:
                payload["data_quality_status"] = DataQualityStatus.STALE.value
                payload["quality_reason"] = (
                    f"Missing candle gap detected before this candle: {gap_seconds:.0f}s."
                )
                return
        previous_close = float(previous.close)
        if previous_close <= 0:
            return
        largest_jump = (
            max(
                abs(float(payload["open"]) - previous_close),
                abs(float(payload["high"]) - previous_close),
                abs(float(payload["low"]) - previous_close),
                abs(float(payload["close"]) - previous_close),
            )
            / previous_close
        )
        if largest_jump >= 0.25:
            payload["data_quality_status"] = DataQualityStatus.SUSPICIOUS_PRICE.value
            payload["quality_reason"] = (
                f"Extreme price jump detected: {largest_jump * 100:.1f}% from prior close."
            )

    def store_market_data_stream_event(
        self,
        *,
        provider: str,
        stream_name: str,
        event_type: str,
        symbol: str | None,
        source_timestamp: datetime,
        payload: dict,
        processed: bool,
        reason: str,
    ) -> models.MarketDataStreamEvent:
        row = models.MarketDataStreamEvent(
            provider=provider,
            stream_name=stream_name,
            event_type=event_type,
            symbol=symbol,
            source_timestamp=source_timestamp,
            payload=payload,
            processed=processed,
            reason=reason,
        )
        self.session.add(row)
        self.session.commit()
        self._archive_raw_payload(
            category="market_stream",
            provider=provider,
            symbol=symbol,
            row_id=row.id,
            source_timestamp=source_timestamp,
            payload=payload,
        )
        return row

    def _archive_raw_payload(
        self,
        *,
        category: str,
        provider: str,
        symbol: str | None,
        row_id: str,
        source_timestamp: datetime,
        payload: dict,
        received_at: datetime | None = None,
        processed_at: datetime | None = None,
    ) -> None:
        bucket = os.getenv("RAW_ARCHIVE_BUCKET", "").strip()
        if not bucket:
            return
        source_timestamp = _as_utc(source_timestamp)
        received_at = _as_utc(received_at)
        processed_at = _as_utc(processed_at)
        key = _raw_archive_key(
            category=category,
            provider=provider,
            symbol=symbol,
            row_id=row_id,
            source_timestamp=source_timestamp,
        )
        archive_payload = {
            "category": category,
            "provider": provider,
            "symbol": symbol,
            "row_id": row_id,
            "source_timestamp": source_timestamp,
            "received_at": received_at,
            "processed_at": processed_at,
            "payload": payload,
        }
        try:
            import boto3

            boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1")).put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(_json_safe(archive_payload), separators=(",", ":")).encode("utf-8"),
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except ModuleNotFoundError:
            self.store_audit_log(
                actor="system",
                event_type="RAW_PAYLOAD_ARCHIVE_SKIPPED",
                entity_type=category,
                entity_id=row_id,
                reason="RAW_ARCHIVE_BUCKET is configured but boto3 is not installed.",
                payload={"bucket": bucket, "key": key},
            )
            return
        except Exception as exc:
            self.store_audit_log(
                actor="system",
                event_type="RAW_PAYLOAD_ARCHIVE_FAILED",
                entity_type=category,
                entity_id=row_id,
                reason=f"Raw payload archive failed: {exc}",
                payload={"bucket": bucket, "key": key},
            )
            return
        self.store_audit_log(
            actor="system",
            event_type="RAW_PAYLOAD_ARCHIVED",
            entity_type=category,
            entity_id=row_id,
            reason="Raw provider payload archived to S3.",
            payload={"bucket": bucket, "key": key, "s3_uri": f"s3://{bucket}/{key}"},
        )

    def _record_raw_ingestion_event(
        self,
        *,
        payload_type: str,
        provider: str,
        symbol: str | None,
        raw_table: str,
        raw_row_id: str,
        raw_payload: dict,
        source_timestamp: datetime,
        received_at: datetime,
        processed_at: datetime,
        status: str = "PROCESSED",
    ) -> models.RawIngestionEvent:
        row = models.RawIngestionEvent(
            payload_type=payload_type,
            provider=provider,
            symbol=symbol.upper() if symbol else None,
            status=status,
            raw_table=raw_table,
            raw_row_id=raw_row_id,
            payload_hash=_payload_hash(raw_payload),
            raw_payload=raw_payload,
            source_timestamp=_as_utc(source_timestamp),
            received_at=_as_utc(received_at),
            processed_at=_as_utc(processed_at),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_scheduler_run(
        self,
        *,
        job_name: str,
        success: bool,
        started_at: datetime,
        finished_at: datetime,
        reason: str,
        payload: dict | None = None,
    ) -> models.SchedulerRun:
        row = models.SchedulerRun(
            job_name=job_name,
            success=success,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(finished_at - started_at).total_seconds() * 1000,
            reason=reason,
            payload=payload,
            source_timestamp=finished_at,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_worker_heartbeat(
        self,
        *,
        worker_name: str,
        status: str,
        last_started_at: datetime | None,
        last_finished_at: datetime | None,
        last_success: bool,
        reason: str,
        payload: dict | None = None,
    ) -> models.WorkerHeartbeat:
        row = self.session.scalar(
            select(models.WorkerHeartbeat).where(models.WorkerHeartbeat.worker_name == worker_name)
        )
        if not row:
            row = models.WorkerHeartbeat(worker_name=worker_name, source_timestamp=_now())
            self.session.add(row)
        row.status = status
        row.last_started_at = last_started_at
        row.last_finished_at = last_finished_at
        row.last_success = last_success
        row.reason = reason
        row.payload = payload
        row.source_timestamp = last_finished_at or _now()
        self.session.commit()
        return row

    def store_data_quality_error(
        self,
        *,
        provider: str,
        symbol: str,
        timeframe: str,
        data_quality_status: str,
        reason: str,
        source_timestamp: datetime,
        payload: dict | None = None,
    ) -> models.DataQualityError:
        row = models.DataQualityError(
            provider=provider,
            symbol=symbol,
            timeframe=timeframe,
            data_quality_status=data_quality_status,
            reason=reason,
            source_timestamp=source_timestamp,
            payload=_json_safe(payload) if payload is not None else None,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_missing_candle_gap(
        self,
        *,
        provider: str,
        symbol: str,
        timeframe: str,
        previous_timestamp: datetime | None,
        current_timestamp: datetime,
        gap_seconds: float,
        repaired: bool,
        reason: str,
    ) -> models.MissingCandleGap:
        row = models.MissingCandleGap(
            provider=provider,
            symbol=symbol.upper(),
            timeframe=timeframe,
            previous_timestamp=previous_timestamp,
            current_timestamp=current_timestamp,
            gap_seconds=gap_seconds,
            repaired=repaired,
            reason=reason,
            source_timestamp=current_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def clean_candles_df(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        provider: str = "yahoo_chart",
        limit: int = 500,
        valid_only: bool = True,
    ) -> pd.DataFrame:
        stmt = (
            select(models.CleanMarketData)
            .where(
                models.CleanMarketData.symbol == symbol.upper(),
                models.CleanMarketData.timeframe == timeframe,
                models.CleanMarketData.provider == provider,
            )
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(limit)
        )
        if valid_only:
            stmt = stmt.where(models.CleanMarketData.data_quality_status == "VALID")
        rows = list(reversed(self.session.scalars(stmt).all()))
        data = [
            {
                "timestamp": row.source_timestamp,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "vwap": row.vwap,
                "trade_count": row.trade_count,
                "provider": row.provider,
                "symbol": row.symbol,
                "raw_market_data_id": row.raw_market_data_id,
                "data_quality_status": row.data_quality_status,
                "quality_reason": row.quality_reason,
            }
            for row in rows
        ]
        frame = pd.DataFrame(data)
        if not frame.empty:
            frame = frame.set_index("timestamp")
        return frame

    def store_feature_snapshot(
        self,
        *,
        symbol: str,
        source_timestamp: datetime,
        feature_version: str,
        snapshot: dict,
    ) -> models.SymbolFeatureSnapshot:
        row = models.SymbolFeatureSnapshot(
            symbol=symbol.upper(),
            source_timestamp=source_timestamp,
            feature_version=feature_version,
            snapshot=snapshot,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_intraday_features(
        self,
        *,
        symbol: str,
        source_timestamp: datetime,
        feature_version: str,
        price: float,
        vwap: float | None,
        atr: float | None,
        relative_volume: float | None,
        gap_pct: float | None,
        volume_spike_score: float | None,
        liquidity_score: float | None,
        spread_score: float | None,
    ) -> models.FeatureIntraday:
        row = models.FeatureIntraday(
            symbol=symbol.upper(),
            source_timestamp=source_timestamp,
            feature_version=feature_version,
            price=price,
            vwap=vwap,
            atr=atr,
            relative_volume=relative_volume,
            gap_pct=gap_pct,
            volume_spike_score=volume_spike_score,
            liquidity_score=liquidity_score,
            spread_score=spread_score,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_daily_features(
        self,
        *,
        symbol: str,
        source_timestamp: datetime,
        feature_version: str,
        atr: float | None,
        atr_pct: float | None,
        gap_pct: float | None,
        trend_score: float | None,
        volatility_score: float | None,
        liquidity_score: float | None,
    ) -> models.FeatureDaily:
        row = models.FeatureDaily(
            symbol=symbol.upper(),
            source_timestamp=source_timestamp,
            feature_version=feature_version,
            atr=atr,
            atr_pct=atr_pct,
            gap_pct=gap_pct,
            trend_score=trend_score,
            volatility_score=volatility_score,
            liquidity_score=liquidity_score,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_market_regime_snapshot(
        self,
        *,
        market_regime: str,
        confidence: float,
        allowed_bias: str,
        risk_multiplier: float,
        breakout_permission: bool,
        mean_reversion_permission: str,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.MarketRegimeSnapshot:
        row = models.MarketRegimeSnapshot(
            market_regime=market_regime,
            confidence=confidence,
            allowed_bias=allowed_bias,
            risk_multiplier=risk_multiplier,
            breakout_permission=breakout_permission,
            mean_reversion_permission=mean_reversion_permission,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_strategy_cooldown(
        self,
        *,
        symbol: str,
        strategy_id: str,
        cooldown_until: datetime,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.StrategyCooldown:
        row = models.StrategyCooldown(
            symbol=symbol.upper(),
            strategy_id=strategy_id,
            cooldown_until=cooldown_until,
            reason=reason,
            is_active=True,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def active_strategy_cooldown(
        self,
        *,
        symbol: str,
        strategy_id: str,
        now: datetime | None = None,
    ) -> models.StrategyCooldown | None:
        now = now or _now()
        return self.session.scalar(
            select(models.StrategyCooldown)
            .where(
                models.StrategyCooldown.symbol == symbol.upper(),
                models.StrategyCooldown.strategy_id == strategy_id,
                models.StrategyCooldown.is_active.is_(True),
                models.StrategyCooldown.cooldown_until > now,
            )
            .order_by(desc(models.StrategyCooldown.cooldown_until))
            .limit(1)
        )

    def recent_accepted_scanner_emission(
        self,
        *,
        symbol: str,
        strategy_id: str,
        within_minutes: int,
        now: datetime | None = None,
    ) -> models.ScannerResult | None:
        now = now or _now()
        cutoff = now - timedelta(minutes=within_minutes)
        return self.session.scalar(
            select(models.ScannerResult)
            .where(
                models.ScannerResult.symbol == symbol.upper(),
                models.ScannerResult.strategy_id == strategy_id,
                models.ScannerResult.accepted.is_(True),
                models.ScannerResult.source_timestamp >= cutoff,
            )
            .order_by(desc(models.ScannerResult.source_timestamp))
            .limit(1)
        )

    def store_scanner_result(
        self,
        decision: ScannerDecision,
        *,
        source_timestamp: datetime,
        payload: dict,
    ) -> models.ScannerResult:
        safe_payload = _json_safe(payload)
        row = models.ScannerResult(
            scanner_name="VWAP_RECLAIM",
            scanner_rule_version=decision.rule_version,
            symbol=decision.symbol,
            strategy_id=decision.strategy_id,
            accepted=decision.accepted,
            score=decision.score,
            reason=decision.reason,
            payload=safe_payload,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.SCANNER,
            outcome=DecisionOutcome.APPROVED if decision.accepted else DecisionOutcome.REJECTED,
            entity_type="scanner_result",
            entity_id=row.id,
            strategy_id=decision.strategy_id,
            rule_version=decision.rule_version,
            reason=decision.reason,
            payload=safe_payload,
            source_timestamp=source_timestamp,
        )
        return row

    def store_generic_scanner_result(
        self,
        *,
        scanner_name: str,
        scanner_rule_version: str,
        symbol: str,
        strategy_id: str | None,
        accepted: bool,
        score: float,
        reason: str,
        payload: dict,
        source_timestamp: datetime | None = None,
    ) -> models.ScannerResult:
        ts = source_timestamp or _now()
        safe_payload = _json_safe(payload)
        row = models.ScannerResult(
            scanner_name=scanner_name,
            scanner_rule_version=scanner_rule_version,
            symbol=symbol.upper(),
            strategy_id=strategy_id,
            accepted=accepted,
            score=score,
            reason=reason,
            payload=safe_payload,
            source_timestamp=ts,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.SCANNER,
            outcome=DecisionOutcome.APPROVED if accepted else DecisionOutcome.REJECTED,
            entity_type="scanner_result",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=scanner_rule_version,
            reason=reason,
            payload=safe_payload,
            source_timestamp=ts,
        )
        return row

    def request_strategy_status_change(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        requested_status: str,
        current_status: str,
        requested_by: str,
        evidence: dict,
        reason: str,
    ) -> models.StrategyApprovalRequest:
        row = models.StrategyApprovalRequest(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            requested_status=requested_status,
            current_status=current_status,
            requested_by=requested_by,
            evidence=evidence,
            reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.STRATEGY,
            outcome=DecisionOutcome.RECORDED,
            entity_type="strategy_approval_request",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=strategy_version,
            reason=reason,
            payload=evidence,
        )
        return row

    def decide_strategy_status_change(
        self,
        *,
        request_id: str,
        approved: bool,
        decided_by: str,
        decision_reason: str,
    ) -> models.StrategyApprovalRequest:
        row = self.session.get(models.StrategyApprovalRequest, request_id)
        if not row:
            raise ValueError(f"Unknown strategy approval request: {request_id}")
        row.status = (
            StrategyApprovalStatus.APPROVED.value
            if approved
            else StrategyApprovalStatus.REJECTED.value
        )
        row.approved_by = decided_by
        row.decision_reason = decision_reason
        row.decided_at = _now()
        if approved:
            strategy = self.session.scalar(
                select(models.StrategyRegistry).where(
                    models.StrategyRegistry.strategy_id == row.strategy_id,
                    models.StrategyRegistry.version == row.strategy_version,
                )
            )
            if strategy:
                strategy.status = row.requested_status
                strategy.changed_reason = decision_reason
        self.session.commit()
        return row

    def store_signal(self, signal: TradeSignal) -> models.Signal:
        existing = self.session.scalar(
            select(models.Signal).where(models.Signal.idempotency_key == signal.idempotency_key)
        )
        if existing:
            return existing
        row = models.Signal(
            idempotency_key=signal.idempotency_key,
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            trade_type=signal.trade_type.value,
            direction=signal.direction.value,
            entry_zone={"low": signal.entry_zone[0], "high": signal.entry_zone[1]},
            stop_loss=signal.stop_loss,
            target_1=signal.target_1,
            target_2=signal.target_2,
            risk_reward=signal.risk_reward,
            confidence_score=signal.confidence_score,
            time_horizon=signal.time_horizon,
            invalidation=signal.invalidation,
            status=signal.status.value,
            signal_rule_version=signal.rule_version,
            source_timestamp=signal.source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.SIGNAL,
            outcome=DecisionOutcome.APPROVED,
            entity_type="signal",
            entity_id=row.id,
            strategy_id=row.strategy_id,
            rule_version=row.signal_rule_version,
            reason="Signal persisted with unique idempotency key.",
            payload={"idempotency_key": row.idempotency_key},
            source_timestamp=row.source_timestamp,
        )
        return row

    def store_signal_version(
        self,
        *,
        signal_id: str,
        version: str,
        change_reason: str,
        payload: dict,
        source_timestamp: datetime,
    ) -> models.SignalVersion:
        row = models.SignalVersion(
            signal_id=signal_id,
            version=version,
            change_reason=change_reason,
            payload=payload,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_trade_thesis(
        self,
        thesis: AIThesis,
        *,
        signal_id: str,
        symbol: str,
        strategy_id: str,
        source_timestamp: datetime,
    ) -> models.TradeThesis:
        row = models.TradeThesis(
            signal_id=signal_id,
            symbol=symbol,
            strategy_id=strategy_id,
            prompt_version=thesis.prompt_version,
            trade_type=thesis.trade_type,
            setup_quality=thesis.setup_quality,
            catalyst_quality=thesis.catalyst_quality,
            confidence=thesis.confidence,
            reason_for_trade=thesis.reason_for_trade,
            invalidation_reason=thesis.invalidation_reason,
            risks=thesis.risks,
            suggested_holding_period=thesis.suggested_holding_period,
            reason="Rule-based thesis generated for dashboard review; not trade authority.",
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.AI_REVIEW,
            outcome=DecisionOutcome.RECORDED,
            entity_type="trade_thesis",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=thesis.prompt_version,
            reason=row.reason or "Thesis recorded.",
            payload={"confidence": thesis.confidence},
            source_timestamp=source_timestamp,
        )
        return row

    def signal_by_id(self, signal_id: str) -> models.Signal | None:
        return self.session.get(models.Signal, signal_id)

    def latest_candidate_signal(self) -> models.Signal | None:
        return self.session.scalar(
            select(models.Signal)
            .where(
                models.Signal.status.in_(
                    [SignalStatus.CANDIDATE.value, SignalStatus.APPROVED.value]
                )
            )
            .order_by(desc(models.Signal.created_at))
        )

    def store_risk_check(
        self,
        risk: RiskDecision,
        *,
        signal_id: str,
        strategy_id: str,
        source_timestamp: datetime,
        payload: dict,
    ) -> models.RiskCheck:
        row = models.RiskCheck(
            signal_id=signal_id,
            approved=risk.approved,
            reason=risk.reason,
            risk_rule_version=risk.risk_rule_version,
            proposed_position_size=risk.position_size,
            risk_amount=risk.risk_amount,
            payload=payload,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.RISK,
            outcome=DecisionOutcome.APPROVED if risk.approved else DecisionOutcome.REJECTED,
            entity_type="risk_check",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=risk.risk_rule_version,
            reason=risk.reason,
            payload=payload,
            source_timestamp=source_timestamp,
        )
        return row

    def store_order(
        self,
        order: PaperOrder,
        *,
        signal_id: str | None,
        strategy_id: str,
        environment_mode: str,
        source_timestamp: datetime,
        broker: str = "alpaca_paper",
        execution_environment: str = ExecutionEnvironment.PAPER.value,
    ) -> models.Order:
        if order.idempotency_key:
            existing = self.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == order.idempotency_key)
            )
            if existing:
                return existing
        row = models.Order(
            signal_id=signal_id,
            idempotency_key=order.idempotency_key or f"rejected-{source_timestamp.timestamp()}",
            environment_mode=environment_mode,
            execution_environment=execution_environment,
            broker=broker,
            symbol=order.symbol,
            side=normalize_order_side(order.side),
            quantity=order.quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
            stop_loss=order.stop_loss,
            status=order.status.value,
            rejection_reason=order.reason if order.status == OrderStatus.REJECTED else None,
            expected_price=order.limit_price,
            submitted_at=_now() if order.status == OrderStatus.SUBMITTED else None,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.APPROVED
            if order.status != OrderStatus.REJECTED
            else DecisionOutcome.BLOCKED,
            entity_type="order",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=f"{broker}_execution_v1",
            reason=order.reason,
            payload={
                "idempotency_key": row.idempotency_key,
                "quantity": row.quantity,
                "broker": broker,
                "execution_environment": execution_environment,
            },
            source_timestamp=source_timestamp,
        )
        return row

    def mark_order_broker_result(
        self,
        *,
        order_id: str,
        broker_order_id: str | None,
        status: str,
        reason: str,
    ) -> models.Order:
        row = self.session.get(models.Order, order_id)
        if not row:
            raise ValueError(f"Unknown order id: {order_id}")
        row.broker_order_id = broker_order_id
        row.status = status
        if status == OrderStatus.REJECTED.value:
            row.rejection_reason = reason
        self.session.commit()
        return row

    def update_order_from_broker(
        self,
        *,
        broker_order: dict,
        environment_mode: str,
    ) -> models.Order | None:
        broker_order_id = str(broker_order.get("id") or "")
        client_order_id = str(broker_order.get("client_order_id") or "")
        if not broker_order_id and not client_order_id:
            return None

        row = None
        if broker_order_id:
            row = self.session.scalar(
                select(models.Order).where(models.Order.broker_order_id == broker_order_id)
            )
        if not row and client_order_id:
            row = self.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == client_order_id)
            )

        if not row:
            symbol = str(broker_order.get("symbol") or "").upper()
            if not symbol:
                return None
            row = models.Order(
                signal_id=None,
                idempotency_key=client_order_id or f"broker-{broker_order_id}",
                environment_mode=environment_mode,
                execution_environment=(
                    ExecutionEnvironment.LIVE.value
                    if environment_mode == "live"
                    else ExecutionEnvironment.PAPER.value
                ),
                broker="alpaca_live" if environment_mode == "live" else "alpaca_paper",
                broker_order_id=broker_order_id or None,
                symbol=symbol,
                side=normalize_order_side(str(broker_order.get("side") or "")),
                quantity=float(broker_order.get("qty") or 0),
                order_type=str(broker_order.get("type") or ""),
                limit_price=_float_or_none(broker_order.get("limit_price")),
                stop_loss=None,
                status=str(broker_order.get("status") or "UNKNOWN").upper(),
                source_timestamp=_parse_provider_time(broker_order.get("updated_at")) or _now(),
            )
            self.session.add(row)

        row.broker_order_id = broker_order_id or row.broker_order_id
        row.status = str(broker_order.get("status") or row.status).upper()
        if row.status == OrderStatus.REJECTED.value:
            row.rejection_reason = str(
                broker_order.get("failed_reason")
                or broker_order.get("reject_reason")
                or broker_order.get("reason")
                or "Broker reported order rejection."
            )
        row.quantity = float(broker_order.get("qty") or row.quantity or 0)
        row.limit_price = _float_or_none(broker_order.get("limit_price")) or row.limit_price
        self.session.commit()
        provider_timestamp = (
            _parse_provider_time(broker_order.get("updated_at"))
            or _parse_provider_time(broker_order.get("filled_at"))
            or _parse_provider_time(broker_order.get("submitted_at"))
            or _parse_provider_time(broker_order.get("created_at"))
            or _now()
        )
        self._archive_raw_payload(
            category="broker_order",
            provider="alpaca_live" if environment_mode == "live" else "alpaca_paper",
            symbol=row.symbol,
            row_id=row.id,
            source_timestamp=provider_timestamp,
            payload={
                "environment_mode": environment_mode,
                "broker_order": _json_safe(broker_order),
            },
        )
        return row

    def store_broker_fill_from_order(
        self,
        *,
        order: models.Order,
        broker_order: dict,
    ) -> models.Fill | None:
        filled_qty = float(broker_order.get("filled_qty") or 0)
        avg_price = _float_or_none(broker_order.get("filled_avg_price"))
        if filled_qty <= 0 or avg_price is None:
            return None
        broker_fill_id = f"{broker_order.get('id')}:{filled_qty}:{avg_price}"
        existing = self.session.scalar(
            select(models.Fill).where(models.Fill.broker_fill_id == broker_fill_id)
        )
        if existing:
            self.store_audit_log(
                actor="system",
                event_type="DUPLICATE_FILL_IGNORED",
                entity_type="fill",
                entity_id=existing.id,
                reason="Duplicate broker fill event ignored before persistence.",
                payload={"broker_fill_id": broker_fill_id, "order_id": order.id},
            )
            return None
        prior_fill_rows = self.session.scalars(
            select(models.Fill).where(models.Fill.order_id == order.id)
        ).all()
        prior_filled_qty = sum(float(fill.quantity or 0.0) for fill in prior_fill_rows)
        incremental_qty = filled_qty - prior_filled_qty
        if incremental_qty <= 0:
            self.store_audit_log(
                actor="system",
                event_type="DUPLICATE_FILL_IGNORED",
                entity_type="order",
                entity_id=order.id,
                reason="Broker cumulative fill quantity did not exceed already persisted fills.",
                payload={
                    "broker_fill_id": broker_fill_id,
                    "broker_filled_qty": filled_qty,
                    "existing_filled_qty": prior_filled_qty,
                },
            )
            return None
        prior_notional = sum(
            float(fill.quantity or 0.0) * float(fill.price or 0.0) for fill in prior_fill_rows
        )
        cumulative_notional = filled_qty * avg_price
        incremental_price = (cumulative_notional - prior_notional) / incremental_qty
        slippage_bps = _calculate_slippage_bps(
            expected_price=order.expected_price or order.limit_price,
            fill_price=incremental_price,
            side=order.side,
        )
        row = models.Fill(
            order_id=order.id,
            broker_fill_id=broker_fill_id,
            symbol=order.symbol,
            quantity=incremental_qty,
            price=incremental_price,
            slippage_bps=slippage_bps,
            commission=0.0,
            source_timestamp=_parse_provider_time(broker_order.get("filled_at")) or _now(),
        )
        self.session.add(row)
        self.session.commit()
        if order.signal_id:
            self.persist_journal_lifecycle_for_signal(signal_id=order.signal_id)
        return row

    def store_broker_sync(
        self,
        *,
        environment_mode: str,
        broker: str,
        success: bool,
        mismatch_detected: bool,
        reason: str,
        payload: dict | None = None,
    ) -> models.SystemLog:
        source_timestamp = _now()
        row = models.SystemLog(
            log_type="BROKER_SYNC",
            entity_type="broker",
            entity_id=broker,
            actor="system",
            status=environment_mode,
            severity="WARNING" if mismatch_detected or not success else "INFO",
            success=success,
            reason=reason,
            payload={
                "environment_mode": environment_mode,
                "broker": broker,
                "success": success,
                "mismatch_detected": mismatch_detected,
                "payload": _json_safe(payload or {}),
            },
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self._archive_raw_payload(
            category="broker_sync",
            provider=broker,
            symbol=None,
            row_id=row.id,
            source_timestamp=row.source_timestamp,
            payload={
                "environment_mode": environment_mode,
                "broker": broker,
                "success": success,
                "mismatch_detected": mismatch_detected,
                "reason": reason,
                "payload": _json_safe(payload or {}),
            },
        )
        return row

    def store_execution_error(
        self,
        *,
        order_id: str | None,
        environment_mode: str,
        error_type: str,
        reason: str,
        payload: dict | None = None,
    ) -> models.SystemLog:
        row = models.SystemLog(
            log_type="EXECUTION_ERROR",
            entity_type="order",
            entity_id=order_id,
            actor="system",
            status=environment_mode,
            severity="ERROR",
            success=False,
            reason=reason,
            payload={
                "order_id": order_id,
                "environment_mode": environment_mode,
                "error_type": error_type,
                "payload": _json_safe(payload or {}),
            },
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.BLOCKED,
            entity_type="execution_error",
            entity_id=row.id,
            strategy_id=None,
            rule_version="execution_error_v1",
            reason=reason,
            payload={"order_id": order_id, "error_type": error_type, "payload": payload or {}},
            source_timestamp=row.source_timestamp,
        )
        return row

    def latest_broker_sync(
        self,
        *,
        environment_mode: str | None = None,
        broker: str | None = None,
    ) -> models.SystemLog | None:
        stmt = select(models.SystemLog).where(models.SystemLog.log_type == "BROKER_SYNC")
        if environment_mode:
            stmt = stmt.where(models.SystemLog.status == environment_mode)
        if broker:
            stmt = stmt.where(models.SystemLog.entity_id == broker)
        return self.session.scalar(stmt.order_by(desc(models.SystemLog.created_at)).limit(1))

    def activate_kill_switch(
        self,
        *,
        event_type: str,
        reason: str,
        payload: dict | None = None,
        actor: str = "system",
    ) -> models.KillSwitchEvent:
        row = models.KillSwitchEvent(
            event_type=event_type,
            active=True,
            reason=reason,
            payload=_json_safe(payload),
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_audit_log(
            actor=actor,
            event_type="KILL_SWITCH_ACTIVATED",
            entity_type="kill_switch_event",
            entity_id=row.id,
            reason=reason,
            payload=payload,
        )
        return row

    def active_kill_switch_count(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(models.KillSwitchEvent)
                .where(models.KillSwitchEvent.active.is_(True))
            )
            or 0
        )

    def resolve_kill_switch(
        self,
        *,
        event_id: str,
        resolution_reason: str,
        actor: str = "system",
    ) -> models.KillSwitchEvent:
        row = self.session.get(models.KillSwitchEvent, event_id)
        if not row:
            raise ValueError(f"Unknown kill switch event: {event_id}")
        row.active = False
        row.resolved_at = _now()
        row.resolution_reason = resolution_reason
        self.session.commit()
        self.store_audit_log(
            actor=actor,
            event_type="KILL_SWITCH_RESOLVED",
            entity_type="kill_switch_event",
            entity_id=row.id,
            reason=resolution_reason,
            payload={"event_type": row.event_type},
        )
        return row

    def store_exposure_snapshot(
        self,
        *,
        account_equity: float,
        total_exposure: float,
        sector_exposure: dict,
        strategy_exposure: dict,
        symbol_exposure: dict,
        reason: str,
    ) -> models.ExposureSnapshot:
        row = models.ExposureSnapshot(
            account_equity=account_equity,
            total_exposure=total_exposure,
            sector_exposure=sector_exposure,
            strategy_exposure=strategy_exposure,
            symbol_exposure=symbol_exposure,
            reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def execution_error_exists(self, *, order_id: str | None, error_type: str) -> bool:
        return bool(
            self.session.scalar(
                select(models.SystemLog.id)
                .where(models.SystemLog.log_type == "EXECUTION_ERROR")
                .where(models.SystemLog.entity_id == order_id)
                .where(models.SystemLog.payload["error_type"].as_string() == error_type)
                .limit(1)
            )
        )

    def store_broker_account_snapshot(
        self,
        *,
        environment_mode: str,
        broker: str,
        account: dict | None,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.BrokerAccountSnapshot:
        account_payload = _json_safe(account or {})
        row = models.BrokerAccountSnapshot(
            environment_mode=environment_mode,
            broker=broker,
            account_id=str(account_payload.get("id") or account_payload.get("account_number") or "")
            or None,
            status=str(account_payload.get("status") or "") or None,
            currency=str(account_payload.get("currency") or "") or None,
            equity=_float_or_none(account_payload.get("equity")),
            cash=_float_or_none(account_payload.get("cash")),
            buying_power=_float_or_none(account_payload.get("buying_power")),
            daytrade_count=_int_or_none(account_payload.get("daytrade_count")),
            pattern_day_trader=_bool_or_none(account_payload.get("pattern_day_trader")),
            payload=account_payload,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        self._archive_raw_payload(
            category="broker_account",
            provider=broker,
            symbol=None,
            row_id=row.id,
            source_timestamp=row.source_timestamp,
            payload={
                "environment_mode": environment_mode,
                "broker": broker,
                "reason": reason,
                "account": account_payload,
            },
        )
        return row

    def latest_broker_account_snapshot(
        self,
        *,
        environment_mode: str | None = None,
        broker: str | None = None,
    ) -> models.BrokerAccountSnapshot | None:
        stmt = select(models.BrokerAccountSnapshot)
        if environment_mode:
            stmt = stmt.where(models.BrokerAccountSnapshot.environment_mode == environment_mode)
        if broker:
            stmt = stmt.where(models.BrokerAccountSnapshot.broker == broker)
        return self.session.scalar(
            stmt.order_by(desc(models.BrokerAccountSnapshot.created_at)).limit(1)
        )

    def broker_equity_loss_pct(
        self,
        *,
        environment_mode: str,
        broker: str,
        lookback: timedelta,
    ) -> float | None:
        cutoff = _now() - lookback
        base_stmt = select(models.BrokerAccountSnapshot).where(
            models.BrokerAccountSnapshot.environment_mode == environment_mode,
            models.BrokerAccountSnapshot.broker == broker,
            models.BrokerAccountSnapshot.source_timestamp >= cutoff,
        )
        baseline = self.session.scalar(
            base_stmt.order_by(
                models.BrokerAccountSnapshot.source_timestamp.asc(),
                models.BrokerAccountSnapshot.created_at.asc(),
            ).limit(1)
        )
        latest = self.session.scalar(
            base_stmt.order_by(
                desc(models.BrokerAccountSnapshot.source_timestamp),
                desc(models.BrokerAccountSnapshot.created_at),
            ).limit(1)
        )
        if (
            not baseline
            or not latest
            or not baseline.equity
            or baseline.equity <= 0
            or latest.equity is None
        ):
            return None
        return max(0.0, (baseline.equity - latest.equity) / baseline.equity * 100)

    def store_raw_news(
        self,
        *,
        provider: str,
        symbol: str | None,
        headline: str,
        url: str | None,
        raw_payload: dict,
        source_timestamp: datetime,
    ) -> models.RawNews:
        source_timestamp = _as_utc(source_timestamp)
        received_at = _now()
        processed_at = _now()
        row = models.RawNews(
            provider=provider,
            symbol=symbol.upper() if symbol else None,
            headline=headline,
            url=url,
            raw_payload=raw_payload,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
        )
        self.session.add(row)
        self.session.commit()
        self._record_raw_ingestion_event(
            payload_type="raw_news",
            provider=provider,
            symbol=symbol,
            raw_table=models.RawNews.__tablename__,
            raw_row_id=row.id,
            raw_payload=raw_payload,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
        )
        self._archive_raw_payload(
            category="news",
            provider=provider,
            symbol=symbol,
            row_id=row.id,
            source_timestamp=source_timestamp,
            received_at=received_at,
            processed_at=processed_at,
            payload=raw_payload,
        )
        return row

    def enqueue_raw_news(self, **kwargs: Any) -> models.RawNews:
        return self.store_raw_news(**kwargs)

    def store_clean_news(
        self,
        *,
        raw_news_id: str,
        provider: str,
        symbol: str | None,
        headline: str,
        normalized_headline_hash: str,
        summary: str | None,
        source_confidence_score: float,
        duplicate_headline: bool,
        rumor_flag: bool,
        reason: str,
        source_timestamp: datetime,
        sentiment_score: float | None = None,
        relevance_score: float | None = None,
    ) -> models.CleanNews:
        existing = self.session.scalar(
            select(models.CleanNews).where(
                models.CleanNews.normalized_headline_hash == normalized_headline_hash,
                models.CleanNews.symbol == symbol,
            )
        )
        if existing:
            existing.duplicate_headline = True
            existing.reason = f"{existing.reason or ''} Duplicate seen again."
            self.session.commit()
            return existing
        row = models.CleanNews(
            raw_news_id=raw_news_id,
            provider=provider,
            symbol=symbol,
            headline=headline,
            normalized_headline_hash=normalized_headline_hash,
            summary=summary,
            source_confidence_score=source_confidence_score,
            duplicate_headline=duplicate_headline,
            rumor_flag=rumor_flag,
            reason=reason,
            sentiment_score=sentiment_score,
            relevance_score=relevance_score,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def seen_news_hashes(self) -> set[str]:
        hashes = self.session.scalars(select(models.CleanNews.normalized_headline_hash)).all()
        return set(hashes)

    def store_raw_filing(
        self,
        *,
        symbol: str | None,
        accession_number: str | None,
        form_type: str | None,
        raw_payload: dict,
        source_timestamp: datetime,
    ) -> models.RawFiling:
        existing = None
        if accession_number:
            existing = self.session.scalar(
                select(models.RawFiling).where(
                    models.RawFiling.accession_number == accession_number
                )
            )
        if existing:
            return existing
        row = models.RawFiling(
            provider="sec_edgar",
            symbol=symbol,
            accession_number=accession_number,
            form_type=form_type,
            raw_payload=raw_payload,
            source_timestamp=_as_utc(source_timestamp),
            received_at=_now(),
            processed_at=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self._archive_raw_payload(
            category="filings",
            provider="sec_edgar",
            symbol=symbol,
            row_id=row.id,
            source_timestamp=source_timestamp,
            received_at=row.received_at,
            processed_at=row.processed_at,
            payload=raw_payload,
        )
        return row

    def store_filing_event(
        self,
        *,
        raw_filing_id: str,
        symbol: str | None,
        form_type: str | None,
        summary: str,
        materiality_score: float,
        reason: str,
        source_timestamp: datetime,
    ) -> models.FilingEvent:
        existing = self.session.scalar(
            select(models.FilingEvent).where(
                models.FilingEvent.raw_filing_id == raw_filing_id,
                models.FilingEvent.form_type == form_type,
            )
        )
        if existing:
            return existing
        row = models.FilingEvent(
            raw_filing_id=raw_filing_id,
            symbol=symbol,
            form_type=form_type,
            summary=summary,
            materiality_score=materiality_score,
            reason=reason,
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_event(
        self,
        *,
        symbol: str | None,
        event_type: str,
        event_time: datetime | None,
        summary: str,
        direction: str,
        materiality_score: float,
        time_horizon: str,
        confidence: float,
        source: str,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.Event:
        row = models.Event(
            symbol=symbol,
            event_type=event_type,
            event_time=event_time,
            summary=summary,
            direction=direction,
            materiality_score=materiality_score,
            time_horizon=time_horizon,
            confidence=confidence,
            source=source,
            reason=reason,
            source_timestamp=source_timestamp or event_time or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_catalyst(
        self,
        *,
        event_id: str | None,
        symbol: str,
        catalyst_type: str,
        direction: str,
        materiality_score: float,
        confidence: float,
        source: str,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.Catalyst:
        row = models.Catalyst(
            event_id=event_id,
            symbol=symbol.upper(),
            catalyst_type=catalyst_type,
            direction=direction,
            materiality_score=materiality_score,
            confidence=confidence,
            source=source,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_news_catalyst_score(
        self,
        *,
        clean_news_id: str,
        symbol: str | None,
        catalyst_type: str,
        source_confidence_score: float,
        materiality_score: float,
        rumor_flag: bool,
        duplicate_headline: bool,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.NewsCatalystScore:
        row = models.NewsCatalystScore(
            clean_news_id=clean_news_id,
            symbol=symbol,
            catalyst_type=catalyst_type,
            source_confidence_score=source_confidence_score,
            materiality_score=materiality_score,
            rumor_flag=rumor_flag,
            duplicate_headline=duplicate_headline,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def upsert_position(
        self,
        *,
        environment_mode: str,
        symbol: str,
        quantity: float,
        average_price: float | None,
        broker_quantity: float | None,
        broker_average_price: float | None,
        reconciliation_status: str,
        reason: str,
    ) -> models.Position:
        row = self.session.scalar(
            select(models.Position).where(
                models.Position.environment_mode == environment_mode,
                models.Position.symbol == symbol.upper(),
            )
        )
        if not row:
            row = models.Position(
                environment_mode=environment_mode,
                symbol=symbol.upper(),
                source_timestamp=_now(),
            )
            self.session.add(row)
        row.quantity = quantity
        row.average_price = average_price
        row.broker_quantity = broker_quantity
        row.broker_average_price = broker_average_price
        row.reconciliation_status = reconciliation_status
        row.reason = reason
        self.session.commit()
        return row

    def position_for(self, *, environment_mode: str, symbol: str) -> models.Position | None:
        return self.session.scalar(
            select(models.Position).where(
                models.Position.environment_mode == environment_mode,
                models.Position.symbol == symbol.upper(),
            )
        )

    def store_decision_log(
        self,
        *,
        decision_type: DecisionType,
        outcome: DecisionOutcome,
        entity_type: str | None,
        entity_id: str | None,
        strategy_id: str | None,
        rule_version: str | None,
        reason: str,
        payload: dict | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        row = models.DecisionLog(
            decision_type=decision_type.value,
            outcome=outcome.value,
            entity_type=entity_type,
            entity_id=entity_id,
            strategy_id=strategy_id,
            rule_version=rule_version,
            reason=reason,
            payload=_json_safe(payload),
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.add(
            models.AuditLog(
                actor="system",
                event_type=f"{decision_type.value}:{outcome.value}",
                entity_type=entity_type,
                entity_id=entity_id,
                reason=reason,
                payload={
                    "strategy_id": strategy_id,
                    "rule_version": rule_version,
                    "decision_payload": _json_safe(payload or {}),
                },
                source_timestamp=source_timestamp or _now(),
            )
        )
        self.session.commit()
        return row

    def store_decision_snapshot(
        self,
        *,
        stage: str,
        decision_type: DecisionType,
        outcome: DecisionOutcome,
        symbol: str,
        strategy_id: str,
        entity_id: str,
        rule_version: str,
        reason: str,
        payload: dict,
        source_timestamp: datetime,
    ) -> models.DecisionLog:
        safe_payload = _json_safe(payload)
        row = models.DecisionLog(
            decision_type=decision_type.value,
            outcome=outcome.value,
            entity_type="decision_snapshot",
            entity_id=entity_id,
            strategy_id=strategy_id,
            rule_version=rule_version,
            reason=reason,
            payload={
                "snapshot_stage": stage,
                "symbol": symbol,
                **safe_payload,
            },
            source_timestamp=source_timestamp,
        )
        self.session.add(row)
        self.session.add(
            models.AuditLog(
                actor="system",
                event_type=f"decision_snapshot:{stage}:{outcome.value}",
                entity_type="decision_snapshot",
                entity_id=entity_id,
                reason=reason,
                payload={
                    "snapshot_stage": stage,
                    "symbol": symbol,
                    "strategy_id": strategy_id,
                    "rule_version": rule_version,
                    "decision_payload": safe_payload,
                },
                source_timestamp=source_timestamp,
            )
        )
        self.session.commit()
        return row

    def list_decision_snapshots(
        self,
        *,
        stage: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
    ) -> list[models.DecisionLog]:
        query = select(models.DecisionLog).where(
            models.DecisionLog.entity_type == "decision_snapshot"
        )
        if entity_id:
            query = query.where(models.DecisionLog.entity_id == entity_id)
        fetch_limit = limit * 5 if stage else limit
        rows = list(
            self.session.scalars(
                query.order_by(desc(models.DecisionLog.created_at)).limit(fetch_limit)
            ).all()
        )
        if stage:
            rows = [
                row
                for row in rows
                if isinstance(row.payload, dict) and row.payload.get("snapshot_stage") == stage
            ]
        return rows[:limit]

    def store_audit_log(
        self,
        *,
        actor: str,
        event_type: str,
        entity_type: str | None,
        entity_id: str | None,
        reason: str,
        payload: dict | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.AuditLog:
        row = models.AuditLog(
            actor=actor,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            payload=_json_safe(payload),
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_journal_entry(
        self,
        *,
        symbol: str,
        strategy_id: str | None,
        entry_thesis: str,
        actual_entry: float | None,
        actual_exit: float | None,
        pnl: float | None,
        human_notes: str | None,
        mistake_tags: list[str],
        change_reason: str,
        signal_id: str | None = None,
        market_regime: str | None = None,
        catalyst: str | None = None,
        max_favorable_excursion: float | None = None,
        max_adverse_excursion: float | None = None,
        slippage_bps: float | None = None,
        time_in_trade_seconds: float | None = None,
        rule_violations: list[str] | None = None,
        ai_review: str | None = None,
    ) -> models.TradeJournal:
        row = models.TradeJournal(
            symbol=symbol.upper(),
            strategy_id=strategy_id,
            signal_id=signal_id,
            entry_thesis=entry_thesis,
            actual_entry=actual_entry,
            actual_exit=actual_exit,
            market_regime=market_regime,
            catalyst=catalyst,
            pnl=pnl,
            max_favorable_excursion=max_favorable_excursion,
            max_adverse_excursion=max_adverse_excursion,
            slippage_bps=slippage_bps,
            time_in_trade_seconds=time_in_trade_seconds,
            rule_violations=rule_violations or [],
            ai_review=ai_review,
            human_notes=human_notes,
            mistake_tags=mistake_tags,
            change_reason=change_reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.JOURNAL,
            outcome=DecisionOutcome.RECORDED,
            entity_type="trade_journal",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version="journal_v1",
            reason=change_reason,
            payload={"symbol": symbol.upper(), "pnl": pnl},
            source_timestamp=row.source_timestamp,
        )
        return row

    def update_journal_lifecycle(
        self,
        journal: models.TradeJournal,
        *,
        actual_entry: float | None = None,
        actual_exit: float | None = None,
        pnl: float | None = None,
        max_favorable_excursion: float | None = None,
        max_adverse_excursion: float | None = None,
        slippage_bps: float | None = None,
        time_in_trade_seconds: float | None = None,
        rule_violations: list[str] | None = None,
        change_reason: str,
    ) -> models.TradeJournal:
        journal.actual_entry = actual_entry
        journal.actual_exit = actual_exit
        journal.pnl = pnl
        journal.max_favorable_excursion = max_favorable_excursion
        journal.max_adverse_excursion = max_adverse_excursion
        journal.slippage_bps = slippage_bps
        journal.time_in_trade_seconds = time_in_trade_seconds
        journal.rule_violations = rule_violations or []
        journal.change_reason = change_reason
        self.session.commit()
        self.store_decision_log(
            decision_type=DecisionType.JOURNAL,
            outcome=DecisionOutcome.CHANGED,
            entity_type="trade_journal",
            entity_id=journal.id,
            strategy_id=journal.strategy_id,
            rule_version="journal_lifecycle_v1",
            reason=change_reason,
            payload={
                "symbol": journal.symbol,
                "actual_entry": actual_entry,
                "actual_exit": actual_exit,
                "pnl": pnl,
                "max_favorable_excursion": max_favorable_excursion,
                "max_adverse_excursion": max_adverse_excursion,
                "slippage_bps": slippage_bps,
                "time_in_trade_seconds": time_in_trade_seconds,
                "rule_violations": rule_violations or [],
            },
        )
        return journal

    def calculate_journal_lifecycle_metrics(self, *, signal_id: str) -> dict[str, Any] | None:
        fills = self.session.execute(
            select(models.Fill, models.Order)
            .join(models.Order, models.Fill.order_id == models.Order.id)
            .where(models.Order.signal_id == signal_id)
            .order_by(models.Fill.source_timestamp.asc(), models.Fill.created_at.asc())
        ).all()
        if not fills:
            return None

        signal = self.session.get(models.Signal, signal_id)
        symbol = signal.symbol if signal else fills[0][1].symbol
        entry_side = self._journal_entry_side(signal=signal, fallback_side=fills[0][1].side)
        exit_side = "buy" if entry_side == "sell" else "sell"
        entry_fills = [(fill, order) for fill, order in fills if order.side.lower() == entry_side]
        exit_fills = [(fill, order) for fill, order in fills if order.side.lower() == exit_side]
        if not entry_fills:
            return None

        entry_quantity = sum(float(fill.quantity or 0.0) for fill, _order in entry_fills)
        exit_quantity = sum(float(fill.quantity or 0.0) for fill, _order in exit_fills)
        if entry_quantity <= 0:
            return None

        actual_entry = _weighted_average(
            [(fill.price, fill.quantity) for fill, _order in entry_fills]
        )
        if actual_entry is None:
            return None
        actual_exit = _weighted_average(
            [(fill.price, fill.quantity) for fill, _order in exit_fills]
        )
        slippage_bps = _weighted_average(
            [
                (fill.slippage_bps, fill.quantity)
                for fill, _order in entry_fills + exit_fills
                if fill.slippage_bps is not None
            ]
        )
        first_entry_at = min(_as_utc(fill.source_timestamp) for fill, _order in entry_fills)
        last_exit_at = (
            max(_as_utc(fill.source_timestamp) for fill, _order in exit_fills)
            if exit_fills
            else None
        )
        latest_candle = self._latest_journal_candle(symbol)
        latest_candle_at = _as_utc(latest_candle.source_timestamp) if latest_candle else None
        fully_exited = exit_quantity >= entry_quantity and exit_quantity > 0
        if fully_exited and last_exit_at is not None:
            end_at = last_exit_at
        else:
            end_at = latest_candle_at or last_exit_at or datetime.now(UTC)

        high, low = self._journal_price_excursion(
            symbol=symbol, start_at=first_entry_at, end_at=end_at
        )
        if high is None and actual_exit is not None:
            high = max(actual_entry, actual_exit)
        if low is None and actual_exit is not None:
            low = min(actual_entry, actual_exit)
        high = high if high is not None else actual_entry
        low = low if low is not None else actual_entry

        direction = signal.direction if signal else Direction.LONG.value
        signed_multiplier = -1.0 if direction == Direction.SHORT.value else 1.0
        exited_quantity = min(exit_quantity, entry_quantity)
        pnl = None
        if actual_exit is not None and exited_quantity > 0:
            commissions = sum((fill.commission or 0.0) for fill, _order in entry_fills + exit_fills)
            pnl = (actual_exit - actual_entry) * exited_quantity * signed_multiplier - commissions

        if direction == Direction.SHORT.value:
            max_favorable = max(0.0, (actual_entry - low) * entry_quantity)
            max_adverse = min(0.0, (actual_entry - high) * entry_quantity)
        else:
            max_favorable = max(0.0, (high - actual_entry) * entry_quantity)
            max_adverse = min(0.0, (low - actual_entry) * entry_quantity)

        open_quantity = max(0.0, entry_quantity - exit_quantity)
        rule_violations = self._journal_rule_violations(
            signal=signal,
            latest_candle=latest_candle,
            first_entry_at=first_entry_at,
            open_quantity=open_quantity,
            entry_side=entry_side,
        )
        if fully_exited:
            change_reason = "Journal lifecycle updated after full exit fill reconciliation."
        elif exit_quantity > 0:
            change_reason = "Journal lifecycle updated after partial exit fill reconciliation."
        else:
            change_reason = "Journal lifecycle updated after entry fill reconciliation."

        return {
            "symbol": symbol,
            "strategy_id": signal.strategy_id if signal else None,
            "signal_id": signal_id,
            "actual_entry": actual_entry,
            "actual_exit": actual_exit,
            "latest_price": latest_candle.close if latest_candle else actual_exit,
            "exit_side": exit_side,
            "open_quantity": open_quantity,
            "pnl": pnl,
            "max_favorable_excursion": max_favorable,
            "max_adverse_excursion": max_adverse,
            "slippage_bps": slippage_bps,
            "time_in_trade_seconds": max(0.0, (end_at - first_entry_at).total_seconds()),
            "rule_violations": rule_violations,
            "change_reason": change_reason,
            "fully_exited": fully_exited,
        }

    def persist_journal_lifecycle_for_signal(self, *, signal_id: str) -> dict[str, Any]:
        metrics = self.calculate_journal_lifecycle_metrics(signal_id=signal_id)
        if not metrics:
            return {"journal": None, "metrics": None, "created": False, "updated": False}

        existing = self.session.scalar(
            select(models.TradeJournal).where(models.TradeJournal.signal_id == signal_id)
        )
        if existing:
            if self._journal_lifecycle_changed(existing, metrics):
                journal = self.update_journal_lifecycle(
                    existing,
                    actual_entry=metrics["actual_entry"],
                    actual_exit=metrics["actual_exit"],
                    pnl=metrics["pnl"],
                    max_favorable_excursion=metrics["max_favorable_excursion"],
                    max_adverse_excursion=metrics["max_adverse_excursion"],
                    slippage_bps=metrics["slippage_bps"],
                    time_in_trade_seconds=metrics["time_in_trade_seconds"],
                    rule_violations=metrics["rule_violations"],
                    change_reason=metrics["change_reason"],
                )
                return {"journal": journal, "metrics": metrics, "created": False, "updated": True}
            return {"journal": existing, "metrics": metrics, "created": False, "updated": False}

        entry_thesis = (
            f"Automatic journal entry from reconciled fills for {metrics['symbol']}. "
            f"Entry={metrics['actual_entry']}; exit={metrics['actual_exit']}."
        )
        regime_snapshot = self.session.scalar(
            select(models.MarketRegimeSnapshot).order_by(
                desc(models.MarketRegimeSnapshot.source_timestamp),
                desc(models.MarketRegimeSnapshot.created_at),
            )
        )
        market_regime = regime_snapshot.market_regime if regime_snapshot else None
        journal = self.store_journal_entry(
            symbol=metrics["symbol"],
            strategy_id=metrics["strategy_id"],
            signal_id=signal_id,
            market_regime=market_regime,
            entry_thesis=entry_thesis,
            actual_entry=metrics["actual_entry"],
            actual_exit=metrics["actual_exit"],
            pnl=metrics["pnl"],
            max_favorable_excursion=metrics["max_favorable_excursion"],
            max_adverse_excursion=metrics["max_adverse_excursion"],
            slippage_bps=metrics["slippage_bps"],
            time_in_trade_seconds=metrics["time_in_trade_seconds"],
            rule_violations=metrics["rule_violations"],
            human_notes=None,
            mistake_tags=[],
            change_reason=metrics["change_reason"],
        )
        return {"journal": journal, "metrics": metrics, "created": True, "updated": False}

    def _latest_journal_candle(self, symbol: str) -> models.CleanMarketData | None:
        return self.session.scalar(
            select(models.CleanMarketData)
            .where(models.CleanMarketData.symbol == symbol.upper())
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(1)
        )

    def _journal_price_excursion(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[float | None, float | None]:
        return self.session.execute(
            select(
                func.max(models.CleanMarketData.high), func.min(models.CleanMarketData.low)
            ).where(
                models.CleanMarketData.symbol == symbol.upper(),
                models.CleanMarketData.source_timestamp >= start_at,
                models.CleanMarketData.source_timestamp <= end_at,
            )
        ).one()

    @staticmethod
    def _journal_entry_side(*, signal: models.Signal | None, fallback_side: str) -> str:
        if signal:
            return entry_side_from_direction(signal.direction)
        return normalize_order_side(fallback_side)

    @staticmethod
    def _journal_lifecycle_changed(journal: models.TradeJournal, metrics: dict[str, Any]) -> bool:
        checks = {
            "actual_entry": metrics["actual_entry"],
            "actual_exit": metrics["actual_exit"],
            "pnl": metrics["pnl"],
            "max_favorable_excursion": metrics["max_favorable_excursion"],
            "max_adverse_excursion": metrics["max_adverse_excursion"],
            "slippage_bps": metrics["slippage_bps"],
            "time_in_trade_seconds": metrics["time_in_trade_seconds"],
            "rule_violations": metrics["rule_violations"],
        }
        for field, expected in checks.items():
            current = getattr(journal, field)
            if isinstance(expected, float) or isinstance(current, float):
                if expected is None or current is None:
                    if expected != current:
                        return True
                elif abs(float(current) - float(expected)) > 0.0001:
                    return True
            elif current != expected:
                return True
        return False

    @staticmethod
    def _journal_rule_violations(
        *,
        signal: models.Signal | None,
        latest_candle: models.CleanMarketData | None,
        first_entry_at: datetime,
        open_quantity: float,
        entry_side: str,
    ) -> list[str]:
        if not signal or open_quantity <= 0:
            return []
        violations = []
        latest_at = _as_utc(latest_candle.source_timestamp) if latest_candle else datetime.now(UTC)
        if (
            signal.trade_type == TradeType.DAY_TRADE.value
            and latest_at.date() > first_entry_at.date()
        ):
            violations.append("DAY_TRADE_TO_SWING_BLOCKED")
        if latest_candle and signal.stop_loss:
            stop_breached = (
                latest_candle.close <= signal.stop_loss
                if entry_side == "buy"
                else latest_candle.close >= signal.stop_loss
            )
            if stop_breached:
                violations.append("STOP_LOSS_BREACHED")
        return violations

    def store_ai_review(
        self,
        *,
        trade_journal_id: str | None,
        prompt_version: str,
        review_text: str,
        confidence_score: float | None,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.AIReview:
        row = models.AIReview(
            trade_journal_id=trade_journal_id,
            prompt_template_id=None,
            prompt_version=prompt_version,
            review_text=review_text,
            confidence_score=confidence_score,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_weekly_review(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        summary: str,
        metrics: dict,
        reason: str,
    ) -> models.WeeklyReview:
        row = models.WeeklyReview(
            week_start=week_start,
            week_end=week_end,
            summary=summary,
            metrics=metrics,
            reason=reason,
            source_timestamp=week_end,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_strategy_recommendation(
        self,
        *,
        strategy_id: str | None,
        recommendation: str,
        reason: str,
    ) -> models.StrategyRecommendation:
        row = models.StrategyRecommendation(
            strategy_id=strategy_id,
            recommendation=recommendation,
            reason=reason,
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_audit_log(
            actor="system",
            event_type="STRATEGY_RECOMMENDATION_CREATED",
            entity_type="strategy_recommendation",
            entity_id=row.id,
            reason=reason,
            payload={"strategy_id": strategy_id, "recommendation": recommendation},
            source_timestamp=row.source_timestamp,
        )
        return row

    def store_backtest_report(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        universe_name: str | None,
        assumptions: dict,
        metrics: dict,
        report_uri: str | None,
        survivorship_bias_warning: str | None,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.BacktestReport:
        row = models.BacktestReport(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            universe_name=universe_name,
            assumptions=assumptions,
            metrics=metrics,
            report_uri=report_uri,
            survivorship_bias_warning=survivorship_bias_warning,
            reason=reason,
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        self.store_audit_log(
            actor="system",
            event_type="BACKTEST_REPORT_STORED",
            entity_type="backtest_report",
            entity_id=row.id,
            reason=reason,
            payload={
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "universe_name": universe_name,
                "report_uri": report_uri,
                "metrics": metrics,
                "survivorship_bias_warning": survivorship_bias_warning,
            },
            source_timestamp=row.source_timestamp,
        )
        return row

    def list_rows(self, model: type, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(model).order_by(desc(model.created_at)).limit(limit)
        ).all()
        return [model_to_dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        tracked_model_names = {
            "symbols": "SymbolUniverse",
            "raw_ingestion_events": "RawIngestionEvent",
            "raw_trade_ticks": "RawTradeTick",
            "clean_candles": "CleanMarketData",
            "stream_events": "MarketDataStreamEvent",
            "provider_health": "ProviderHealthSnapshot",
            "provider_rate_limits": "ProviderRateLimitState",
            "worker_heartbeats": "WorkerHeartbeat",
            "clean_news": "CleanNews",
            "filings": "RawFiling",
            "scanner_results": "ScannerResult",
            "signals": "Signal",
            "risk_checks": "RiskCheck",
            "broker_account_snapshots": "BrokerAccountSnapshot",
            "orders": "Order",
            "fills": "Fill",
            "positions": "Position",
            "system_logs": "SystemLog",
            "journal_entries": "TradeJournal",
            "scheduler_runs": "SchedulerRun",
            "live_readiness_reports": "LiveReadinessReport",
            "strategy_approval_requests": "StrategyApprovalRequest",
            "kill_switches": "KillSwitchEvent",
            "weekly_reviews": "WeeklyReview",
            "recommendations": "StrategyRecommendation",
            "pit_universe_memberships": "PointInTimeUniverseMembership",
            "short_interest_snapshots": "ShortInterestSnapshot",
            "options_intelligence_snapshots": "OptionsIntelligenceSnapshot",
            "multi_bagger_candidate_scores": "MultiBaggerCandidateScore",
        }
        counts = {}
        for name, model_name in tracked_model_names.items():
            model = getattr(models, model_name, None)
            if model is None:
                counts[name] = 0
                continue
            counts[name] = int(self.session.scalar(select(func.count()).select_from(model)) or 0)
        counts["execution_errors"] = int(
            self.session.scalar(
                select(func.count())
                .select_from(models.SystemLog)
                .where(models.SystemLog.log_type == "EXECUTION_ERROR")
            )
            or 0
        )
        counts["broker_sync_logs"] = int(
            self.session.scalar(
                select(func.count())
                .select_from(models.SystemLog)
                .where(models.SystemLog.log_type == "BROKER_SYNC")
            )
            or 0
        )
        return counts

    def latest_clean_candles(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.CleanMarketData, limit)

    def latest_features(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.FeatureIntraday, limit)

    def store_opportunity_score(
        self,
        *,
        scanner_result_id: str | None,
        signal_id: str | None,
        symbol: str,
        strategy_id: str,
        setup_type: str | None,
        score: float,
        grade: str,
        component_scores: dict[str, float],
        components: list[dict[str, Any]],
        penalties: list[dict[str, Any]],
        explanation: str,
        expected_r: float | None = None,
        historical_win_rate: float | None = None,
        expectancy_sample_size: int = 0,
        confidence_level: float = 0.0,
        suggested_risk_multiplier: float = 0.0,
        market_regime: str | None = None,
        sector_regime: str | None = None,
        catalyst_type: str | None = None,
        linked_news_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.OpportunityScore:
        ts = source_timestamp or _now()
        row = models.OpportunityScore(
            scanner_result_id=scanner_result_id,
            signal_id=signal_id,
            symbol=symbol.upper(),
            strategy_id=strategy_id,
            setup_type=setup_type,
            score=score,
            grade=grade,
            component_scores=_json_safe(component_scores),
            penalties=_json_safe(penalties),
            explanation=explanation,
            expected_r=expected_r,
            historical_win_rate=historical_win_rate,
            expectancy_sample_size=expectancy_sample_size,
            confidence_level=confidence_level,
            suggested_risk_multiplier=suggested_risk_multiplier,
            market_regime=market_regime,
            sector_regime=sector_regime,
            catalyst_type=catalyst_type,
            linked_news_id=linked_news_id,
            payload=_json_safe(payload or {}),
            source_timestamp=ts,
        )
        self.session.add(row)
        self.session.flush()
        for component in components:
            self.session.add(
                models.OpportunityScoreComponent(
                    opportunity_score_id=row.id,
                    component_name=str(component["component_name"]),
                    raw_value=component.get("raw_value"),
                    score=float(component.get("score", 0.0)),
                    weight=float(component.get("weight", 0.0)),
                    explanation=component.get("explanation"),
                    source_timestamp=ts,
                )
            )
        self.session.commit()
        return row

    def store_alpha_rejection_reason(
        self,
        *,
        scanner_result_id: str | None,
        symbol: str,
        strategy_id: str | None,
        setup_type: str | None,
        reason_code: str,
        reason: str,
        severity: str = "BLOCKER",
        payload: dict[str, Any] | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.AlphaRejectionReason:
        row = models.AlphaRejectionReason(
            scanner_result_id=scanner_result_id,
            symbol=symbol.upper(),
            strategy_id=strategy_id,
            setup_type=setup_type,
            reason_code=reason_code,
            reason=reason,
            severity=severity,
            payload=_json_safe(payload or {}),
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_expectancy_snapshot(
        self,
        *,
        bucket_type: str,
        bucket_key: str,
        strategy_id: str | None,
        setup_type: str | None,
        sample_size: int,
        win_rate: float | None,
        average_win: float | None,
        average_loss: float | None,
        expectancy_r: float | None,
        profit_factor: float | None,
        max_drawdown: float | None,
        average_hold_seconds: float | None,
        average_slippage_bps: float | None,
        average_mfe: float | None,
        average_mae: float | None,
        confidence_level: float,
        payload: dict[str, Any],
        source_timestamp: datetime | None = None,
    ) -> models.ExpectancySnapshot:
        row = models.ExpectancySnapshot(
            bucket_type=bucket_type,
            bucket_key=bucket_key,
            strategy_id=strategy_id,
            setup_type=setup_type,
            sample_size=sample_size,
            win_rate=win_rate,
            average_win=average_win,
            average_loss=average_loss,
            expectancy_r=expectancy_r,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            average_hold_seconds=average_hold_seconds,
            average_slippage_bps=average_slippage_bps,
            average_mfe=average_mfe,
            average_mae=average_mae,
            confidence_level=confidence_level,
            payload=_json_safe(payload),
            source_timestamp=source_timestamp or _now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def store_strategy_performance_bucket(self, **kwargs: Any) -> models.StrategyPerformanceBucket:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        row = models.StrategyPerformanceBucket(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def store_sector_strength_snapshot(self, **kwargs: Any) -> models.SectorStrengthSnapshot:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        row = models.SectorStrengthSnapshot(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def store_symbol_relative_strength_snapshot(
        self, **kwargs: Any
    ) -> models.SymbolRelativeStrengthSnapshot:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        row = models.SymbolRelativeStrengthSnapshot(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def store_point_in_time_universe_membership(
        self, **kwargs: Any
    ) -> models.PointInTimeUniverseMembership:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        if "symbol" in kwargs:
            kwargs["symbol"] = str(kwargs["symbol"]).upper()
        row = models.PointInTimeUniverseMembership(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(models.PointInTimeUniverseMembership).where(
                    models.PointInTimeUniverseMembership.universe_name == row.universe_name,
                    models.PointInTimeUniverseMembership.as_of_date == row.as_of_date,
                    models.PointInTimeUniverseMembership.symbol == row.symbol,
                )
            )
            if existing:
                return existing
            raise
        return row

    def point_in_time_universe(
        self,
        *,
        as_of: datetime,
        universe_name: str = "tradable_us_equities",
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        query = select(models.PointInTimeUniverseMembership).where(
            models.PointInTimeUniverseMembership.universe_name == universe_name,
            models.PointInTimeUniverseMembership.as_of_date <= _as_utc(as_of),
        )
        if active_only:
            query = query.where(
                models.PointInTimeUniverseMembership.is_active.is_(True),
                models.PointInTimeUniverseMembership.delisted.is_(False),
            )
        rows = self.session.scalars(
            query.order_by(
                models.PointInTimeUniverseMembership.symbol.asc(),
                desc(models.PointInTimeUniverseMembership.as_of_date),
            )
        ).all()
        latest_by_symbol: dict[str, models.PointInTimeUniverseMembership] = {}
        for row in rows:
            latest_by_symbol.setdefault(row.symbol, row)
        return [model_to_dict(row) for row in latest_by_symbol.values()]

    def store_short_interest_snapshot(self, **kwargs: Any) -> models.ShortInterestSnapshot:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        if "symbol" in kwargs:
            kwargs["symbol"] = str(kwargs["symbol"]).upper()
        row = models.ShortInterestSnapshot(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def store_options_intelligence_snapshot(
        self, **kwargs: Any
    ) -> models.OptionsIntelligenceSnapshot:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        if "symbol" in kwargs:
            kwargs["symbol"] = str(kwargs["symbol"]).upper()
        row = models.OptionsIntelligenceSnapshot(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def store_multi_bagger_candidate_score(self, **kwargs: Any) -> models.MultiBaggerCandidateScore:
        if "payload" in kwargs:
            kwargs["payload"] = _json_safe(kwargs["payload"])
        if "risk_flags" in kwargs:
            kwargs["risk_flags"] = _json_safe(kwargs["risk_flags"])
        if "component_scores" in kwargs:
            kwargs["component_scores"] = _json_safe(kwargs["component_scores"])
        if "symbol" in kwargs:
            kwargs["symbol"] = str(kwargs["symbol"]).upper()
        row = models.MultiBaggerCandidateScore(**kwargs)
        if row.source_timestamp is None:
            row.source_timestamp = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def latest_short_interest_for(self, symbol: str) -> models.ShortInterestSnapshot | None:
        return self.session.scalar(
            select(models.ShortInterestSnapshot)
            .where(models.ShortInterestSnapshot.symbol == symbol.upper())
            .order_by(
                desc(models.ShortInterestSnapshot.source_timestamp),
                desc(models.ShortInterestSnapshot.created_at),
            )
            .limit(1)
        )

    def latest_options_intelligence_for(
        self, symbol: str
    ) -> models.OptionsIntelligenceSnapshot | None:
        return self.session.scalar(
            select(models.OptionsIntelligenceSnapshot)
            .where(models.OptionsIntelligenceSnapshot.symbol == symbol.upper())
            .order_by(
                desc(models.OptionsIntelligenceSnapshot.source_timestamp),
                desc(models.OptionsIntelligenceSnapshot.created_at),
            )
            .limit(1)
        )

    def latest_point_in_time_universe_memberships(self, limit: int = 100) -> list[dict[str, Any]]:
        if not hasattr(models, "PointInTimeUniverseMembership"):
            return []
        return self.list_rows(models.PointInTimeUniverseMembership, limit)

    def latest_short_interest_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ShortInterestSnapshot, limit)

    def latest_options_intelligence_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.OptionsIntelligenceSnapshot, limit)

    def latest_multi_bagger_candidate_scores(self, limit: int = 100) -> list[dict[str, Any]]:
        if not hasattr(models, "MultiBaggerCandidateScore"):
            return []
        return self.list_rows(models.MultiBaggerCandidateScore, limit)

    def latest_opportunity_scores(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.OpportunityScore, limit)

    def latest_opportunity_scores_for_symbol(
        self, symbol: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.OpportunityScore)
            .where(models.OpportunityScore.symbol == symbol.upper())
            .order_by(
                desc(models.OpportunityScore.source_timestamp),
                desc(models.OpportunityScore.created_at),
            )
            .limit(limit)
        ).all()
        return [model_to_dict(row) for row in rows]

    def opportunity_score_components(self, opportunity_score_id: str) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.OpportunityScoreComponent)
            .where(models.OpportunityScoreComponent.opportunity_score_id == opportunity_score_id)
            .order_by(models.OpportunityScoreComponent.component_name.asc())
        ).all()
        return [model_to_dict(row) for row in rows]

    def latest_alpha_rejections(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.AlphaRejectionReason, limit)

    def latest_expectancy_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ExpectancySnapshot, limit)

    def latest_strategy_performance_buckets(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.StrategyPerformanceBucket, limit)

    def latest_sector_strength(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.SectorStrengthSnapshot, limit)

    def latest_symbol_relative_strength(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.SymbolRelativeStrengthSnapshot, limit)

    def latest_daily_features(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.FeatureDaily, limit)

    def latest_features_for(self, symbol: str) -> models.FeatureIntraday | None:
        return self.session.scalar(
            select(models.FeatureIntraday)
            .where(models.FeatureIntraday.symbol == symbol.upper())
            .order_by(
                desc(models.FeatureIntraday.source_timestamp),
                desc(models.FeatureIntraday.created_at),
            )
            .limit(1)
        )

    def latest_daily_feature_for(self, symbol: str) -> models.FeatureDaily | None:
        return self.session.scalar(
            select(models.FeatureDaily)
            .where(models.FeatureDaily.symbol == symbol.upper())
            .order_by(
                desc(models.FeatureDaily.source_timestamp), desc(models.FeatureDaily.created_at)
            )
            .limit(1)
        )

    def latest_regime_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.MarketRegimeSnapshot, limit)

    def latest_scanner_results(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ScannerResult, limit)

    def latest_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Signal, limit)

    def latest_trade_theses(self, limit: int = 100) -> list[dict[str, Any]]:
        if not hasattr(models, "TradeThesis"):
            return []
        return self.list_rows(models.TradeThesis, limit)

    def latest_risk_checks(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.RiskCheck, limit)

    def latest_broker_account_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.BrokerAccountSnapshot, limit)

    def latest_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Order, limit)

    def latest_positions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Position, limit)

    def latest_journal(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.TradeJournal, limit)

    def latest_ai_reviews(self, limit: int = 100) -> list[dict[str, Any]]:
        if not hasattr(models, "AIReview"):
            return []
        return self.list_rows(models.AIReview, limit)

    def latest_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.DecisionLog, limit)

    def latest_api_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ApiCallLog, limit)

    def latest_audit_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.AuditLog, limit)

    def latest_data_quality_errors(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.DataQualityError, limit)

    def latest_provider_health(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ProviderHealthSnapshot, limit)

    def latest_provider_rate_limits(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ProviderRateLimitState, limit)

    def latest_worker_heartbeats(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.WorkerHeartbeat, limit)

    def latest_stream_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.MarketDataStreamEvent, limit)

    def latest_scheduler_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.SchedulerRun, limit)

    def last_scheduler_run_times(self) -> dict[str, datetime]:
        rows = self.session.execute(
            select(
                models.SchedulerRun.job_name,
                func.max(models.SchedulerRun.finished_at),
            ).group_by(models.SchedulerRun.job_name)
        ).all()
        return {job_name: finished for job_name, finished in rows if finished is not None}

    def latest_clean_news(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.CleanNews, limit)

    def latest_filings(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.RawFiling, limit)

    def latest_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Fill, limit)

    def latest_broker_sync_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.SystemLog)
            .where(models.SystemLog.log_type == "BROKER_SYNC")
            .order_by(desc(models.SystemLog.created_at))
            .limit(limit)
        ).all()
        result = []
        for row in rows:
            data = model_to_dict(row)
            if isinstance(row.payload, dict):
                data.update(
                    {
                        "environment_mode": row.payload.get("environment_mode"),
                        "broker": row.payload.get("broker"),
                        "mismatch_detected": row.payload.get("mismatch_detected"),
                        "payload": row.payload.get("payload", {}),
                    }
                )
            result.append(data)
        return result

    def latest_execution_errors(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.SystemLog)
            .where(models.SystemLog.log_type == "EXECUTION_ERROR")
            .order_by(desc(models.SystemLog.created_at))
            .limit(limit)
        ).all()
        result = []
        for row in rows:
            data = model_to_dict(row)
            if isinstance(row.payload, dict):
                data.update(
                    {
                        "order_id": row.payload.get("order_id"),
                        "environment_mode": row.payload.get("environment_mode"),
                        "error_type": row.payload.get("error_type"),
                        "payload": row.payload.get("payload", {}),
                    }
                )
            result.append(data)
        return result

    def store_live_readiness_report(
        self,
        *,
        overall_status: str,
        live_allowed: bool,
        reason: str,
        checks: list[dict],
        actor: str = "system",
        source_timestamp: datetime | None = None,
    ) -> models.SystemLog:
        ts = source_timestamp or _now()
        safe_checks = _json_safe(checks)
        row = models.SystemLog(
            log_type="LIVE_READINESS_REPORT",
            entity_type="live_readiness_report",
            entity_id=None,
            actor=actor,
            status=overall_status,
            severity="INFO" if live_allowed else "WARNING",
            success=live_allowed,
            reason=reason,
            payload={
                "overall_status": overall_status,
                "live_allowed": live_allowed,
                "checks": safe_checks,
            },
            source_timestamp=ts,
        )
        self.session.add(row)
        self.session.flush()
        row.entity_id = row.id
        self.session.add(
            models.AuditLog(
                actor=actor,
                event_type="LIVE_READINESS_REPORT",
                entity_type="live_readiness_report",
                entity_id=row.id,
                reason=reason,
                payload={"overall_status": overall_status, "live_allowed": live_allowed},
                source_timestamp=ts,
            )
        )
        self.session.commit()
        return row

    def store_live_trading_approval(
        self,
        *,
        approved_by: str,
        reason: str,
        expires_at: datetime | None,
    ) -> models.SystemLog:
        row = models.SystemLog(
            log_type="LIVE_TRADING_APPROVAL",
            entity_type="live_trading_approval",
            entity_id=None,
            actor=approved_by,
            status=LiveApprovalStatus.ACTIVE.value,
            severity="INFO",
            success=True,
            reason=reason,
            payload={
                "approved_by": approved_by,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "revoked_at": None,
                "revoke_reason": None,
            },
            source_timestamp=_now(),
        )
        self.session.add(row)
        self.session.commit()
        row.entity_id = row.id
        self.session.commit()
        return row

    def active_live_trading_approval(self) -> models.SystemLog | None:
        now = _now()
        self.expire_live_trading_approvals(now=now)
        rows = self.session.scalars(
            select(models.SystemLog)
            .where(
                models.SystemLog.log_type == "LIVE_TRADING_APPROVAL",
                models.SystemLog.status == LiveApprovalStatus.ACTIVE.value,
            )
            .order_by(desc(models.SystemLog.created_at))
        ).all()
        for row in rows:
            expires_at = None
            if isinstance(row.payload, dict) and row.payload.get("expires_at"):
                expires_at = datetime.fromisoformat(row.payload["expires_at"])
            if expires_at is None or expires_at > now:
                return row
        return None

    def expire_live_trading_approvals(self, *, now: datetime | None = None) -> int:
        ts = now or _now()
        rows = self.session.scalars(
            select(models.SystemLog).where(
                models.SystemLog.log_type == "LIVE_TRADING_APPROVAL",
                models.SystemLog.status == LiveApprovalStatus.ACTIVE.value,
            )
        ).all()
        expired = []
        for row in rows:
            expires_at = None
            if isinstance(row.payload, dict) and row.payload.get("expires_at"):
                expires_at = datetime.fromisoformat(row.payload["expires_at"])
            if expires_at is not None and expires_at <= ts:
                row.status = LiveApprovalStatus.EXPIRED.value
                row.success = False
                payload = dict(row.payload or {})
                payload["revoked_at"] = ts.isoformat()
                payload["revoke_reason"] = "Live approval expired automatically."
                row.payload = payload
                expired.append(row)
        if expired:
            self.session.commit()
            for row in expired:
                payload = row.payload or {}
                self.store_audit_log(
                    actor="system",
                    event_type="LIVE_APPROVAL_EXPIRED",
                    entity_type="live_trading_approval",
                    entity_id=row.id,
                    reason="Live approval expired automatically.",
                    payload={
                        "approved_by": payload.get("approved_by"),
                        "expires_at": payload.get("expires_at"),
                    },
                )
        return len(expired)

    def revoke_live_trading_approval(
        self,
        *,
        approval_id: str,
        revoked_by: str,
        reason: str,
    ) -> models.SystemLog:
        row = self.session.get(models.SystemLog, approval_id)
        if not row or row.log_type != "LIVE_TRADING_APPROVAL":
            raise ValueError(f"Unknown live trading approval: {approval_id}")
        row.status = LiveApprovalStatus.REVOKED.value
        row.success = False
        payload = dict(row.payload or {})
        payload["revoked_at"] = _now().isoformat()
        payload["revoke_reason"] = reason
        row.payload = payload
        self.session.commit()
        self.store_audit_log(
            actor=revoked_by,
            event_type="LIVE_APPROVAL_REVOKED",
            entity_type="live_trading_approval",
            entity_id=row.id,
            reason=reason,
            payload={"approved_by": payload.get("approved_by")},
        )
        return row

    def latest_live_readiness_report(self) -> models.SystemLog | None:
        return self.session.scalar(
            select(models.SystemLog)
            .where(models.SystemLog.log_type == "LIVE_READINESS_REPORT")
            .order_by(desc(models.SystemLog.created_at))
            .limit(1)
        )

    def latest_provider_health_for(
        self, provider_name: str
    ) -> models.ProviderHealthSnapshot | None:
        return self.session.scalar(
            select(models.ProviderHealthSnapshot)
            .where(models.ProviderHealthSnapshot.provider_name == provider_name)
            .order_by(desc(models.ProviderHealthSnapshot.created_at))
            .limit(1)
        )

    def latest_healthy_provider(self, provider_name: str) -> bool:
        row = self.latest_provider_health_for(provider_name)
        return bool(row and row.status == ProviderHealthStatus.HEALTHY.value)

    def latest_live_readiness_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.SystemLog)
            .where(models.SystemLog.log_type == "LIVE_READINESS_REPORT")
            .order_by(desc(models.SystemLog.created_at))
            .limit(limit)
        ).all()
        return [model_to_dict(row) for row in rows]

    def latest_live_trading_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(models.SystemLog)
            .where(models.SystemLog.log_type == "LIVE_TRADING_APPROVAL")
            .order_by(desc(models.SystemLog.created_at))
            .limit(limit)
        ).all()
        return [model_to_dict(row) for row in rows]

    def latest_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Event, limit)

    def latest_catalysts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.Catalyst, limit)

    def latest_weekly_reviews(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.list_rows(models.WeeklyReview, limit)

    def latest_strategy_recommendations(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.StrategyRecommendation, limit)

    def latest_backtest_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_rows(models.BacktestReport, limit)

    def latest_kill_switches(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.KillSwitchEvent, limit)

    def latest_exposure_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.ExposureSnapshot, limit)

    def latest_strategy_approval_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.StrategyApprovalRequest, limit)

    def latest_missing_candle_gaps(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_rows(models.MissingCandleGap, limit)


def _parse_provider_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _bool_or_none(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _weighted_average(values: list[tuple[float | None, float]]) -> float | None:
    weighted_values = [
        (value, weight) for value, weight in values if value is not None and weight > 0
    ]
    total_weight = sum(weight for _value, weight in weighted_values)
    if total_weight <= 0:
        return None
    return (
        sum(float(value) * weight for value, weight in weighted_values if value is not None)
        / total_weight
    )


def _calculate_slippage_bps(
    *,
    expected_price: float | None,
    fill_price: float,
    side: str,
) -> float | None:
    if not expected_price or expected_price <= 0:
        return None
    normalized_side = (side or "").lower()
    if normalized_side == "sell":
        return (expected_price - fill_price) / expected_price * 10_000
    return (fill_price - expected_price) / expected_price * 10_000


def _raw_archive_key(
    *,
    category: str,
    provider: str,
    symbol: str | None,
    row_id: str,
    source_timestamp: datetime,
) -> str:
    timestamp = source_timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    date_path = timestamp.strftime("%Y/%m/%d")
    timestamp_part = _archive_key_part(timestamp.isoformat())
    symbol_part = _archive_key_part(symbol or "none")
    return (
        f"raw/{_archive_key_part(category)}/provider={_archive_key_part(provider)}/"
        f"symbol={symbol_part}/date={date_path}/{timestamp_part}-{_archive_key_part(row_id)}.json"
    )


def _archive_key_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timeframe_seconds(timeframe: str) -> int | None:
    normalized = timeframe.strip().lower()
    if normalized.endswith("min"):
        return int(normalized.removesuffix("min")) * 60
    if normalized.endswith("m") and normalized[:-1].isdigit():
        return int(normalized[:-1]) * 60
    if normalized.endswith("d") and normalized[:-1].isdigit():
        return int(normalized[:-1]) * 86_400
    return {"1d": 86_400, "day": 86_400, "daily": 86_400}.get(normalized)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value
