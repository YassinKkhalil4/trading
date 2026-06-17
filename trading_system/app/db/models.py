from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_system.app.core.enums import (
    AdminRole,
    CatalystDirection,
    DataQualityStatus,
    DecisionOutcome,
    DecisionType,
    Direction,
    EnvironmentMode,
    ExecutionEnvironment,
    MarketRegime,
    OrderStatus,
    ProviderReliabilityLevel,
    RecommendationStatus,
    SignalStatus,
    StrategyStatus,
    TradeType,
)
from trading_system.app.db.base import Base


JSONB = JSON().with_variant(_PG_JSONB, "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SourceTimestampMixin:
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))


class ProviderCapability(IdMixin, TimestampMixin, Base):
    __tablename__ = "provider_capabilities"

    provider_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    allowed_usage: Mapped[str] = mapped_column(Text)
    rate_limit_notes: Mapped[str] = mapped_column(Text, default="")
    reliability_level: Mapped[str] = mapped_column(
        String(32), default=ProviderReliabilityLevel.UNKNOWN.value
    )
    live_trading_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    intraday_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    enrichment_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)


class AdminUser(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default=AdminRole.VIEWER.value, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class AdminSession(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "admin_sessions"

    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class ProviderHealthSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "provider_health_snapshots"

    provider_name: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    freshness_seconds: Mapped[float | None] = mapped_column(Float)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class ProviderRateLimitState(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "provider_rate_limit_state"

    provider_name: Mapped[str] = mapped_column(String(80), index=True)
    endpoint: Mapped[str | None] = mapped_column(Text)
    limit_remaining: Mapped[int | None] = mapped_column(Integer)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class SymbolUniverse(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "symbol_universe"

    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    asset_class: Mapped[str] = mapped_column(String(32), default="US_EQUITY")
    exchange: Mapped[str | None] = mapped_column(String(32))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_tradable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_liquid: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    disable_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_asset_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), index=True)
    last_provider_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_price: Mapped[float | None] = mapped_column(Float)
    average_volume: Mapped[float | None] = mapped_column(Float)
    dollar_volume: Mapped[float | None] = mapped_column(Float)
    spread_bps: Mapped[float | None] = mapped_column(Float)
    liquidity_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    raw_asset_payload: Mapped[dict | None] = mapped_column(JSONB)
    tradability_reason: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)


class RawMarketData(TimestampMixin, Base):
    __tablename__ = "raw_market_data"
    __table_args__ = (
        Index("ix_raw_market_data_symbol_time", "symbol", "source_timestamp"),
        Index("ix_raw_market_data_raw_payload_gin", "raw_payload", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (source_timestamp)"},
    )

    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB)

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.symbol}:{self.timeframe}:{self.source_timestamp.isoformat()}"
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RawTradeTick(TimestampMixin, Base):
    __tablename__ = "raw_trade_ticks"
    __table_args__ = (
        Index("ix_raw_trade_ticks_symbol_time", "symbol", "source_timestamp"),
        Index("ix_raw_trade_ticks_raw_payload_gin", "raw_payload", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (source_timestamp)"},
    )

    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(128), primary_key=True, default="")

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.symbol}:{self.source_timestamp.isoformat()}:{self.trade_id}"
    price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    exchange: Mapped[str | None] = mapped_column(String(64))
    conditions: Mapped[list[str] | None] = mapped_column(JSONB)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RawIngestionEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_ingestion_events"
    __table_args__ = (
        Index("ix_raw_ingestion_events_type_status", "payload_type", "status"),
        Index("ix_raw_ingestion_events_provider_time", "provider", "source_timestamp"),
        Index("ix_raw_ingestion_events_raw_payload_gin", "raw_payload", postgresql_using="gin"),
    )

    payload_type: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PROCESSED", index=True)
    raw_table: Mapped[str] = mapped_column(String(80))
    raw_row_id: Mapped[str] = mapped_column(String(256), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class CleanMarketData(TimestampMixin, Base):
    __tablename__ = "clean_market_data"
    __table_args__ = (
        Index("ix_clean_market_data_symbol_time", "symbol", "source_timestamp"),
        Index("ix_clean_market_data_symbol_time_desc", "symbol", text("source_timestamp DESC")),
    )

    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    raw_market_data_id: Mapped[str | None] = mapped_column(String(256))

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.symbol}:{self.timeframe}:{self.source_timestamp.isoformat()}"
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    vwap: Mapped[float | None] = mapped_column(Float)
    data_quality_status: Mapped[str] = mapped_column(
        String(32), default=DataQualityStatus.VALID.value, index=True
    )
    quality_reason: Mapped[str | None] = mapped_column(Text)


class MarketDataStreamEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "market_data_stream_events"

    provider: Mapped[str] = mapped_column(String(80), index=True)
    stream_name: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class RawNews(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_news"
    __table_args__ = (
        Index("ix_raw_news_raw_payload_gin", "raw_payload", postgresql_using="gin"),
    )

    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class CleanNews(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "clean_news"

    raw_news_id: Mapped[str | None] = mapped_column(ForeignKey("raw_news.id"))
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    headline: Mapped[str] = mapped_column(Text)
    normalized_headline_hash: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    source_confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_headline: Mapped[bool] = mapped_column(Boolean, default=False)
    rumor_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    relevance_score: Mapped[float | None] = mapped_column(Float)


class RawFiling(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_filings"
    __table_args__ = (
        Index("ix_raw_filings_raw_payload_gin", "raw_payload", postgresql_using="gin"),
    )

    provider: Mapped[str] = mapped_column(String(80), default="sec_edgar", index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    form_type: Mapped[str | None] = mapped_column(String(32), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ApiCallLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "api_call_logs"

    provider: Mapped[str] = mapped_column(String(80), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    request_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)


class SchedulerRun(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "scheduler_runs"

    job_name: Mapped[str] = mapped_column(String(100), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class WorkerHeartbeat(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "worker_heartbeats"

    worker_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class DataQualityError(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "data_quality_errors"

    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(16))
    data_quality_status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class MissingCandleGap(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "missing_candle_gaps"

    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    previous_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    gap_seconds: Mapped[float] = mapped_column(Float)
    repaired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)


class FeatureIntraday(TimestampMixin, Base):
    __tablename__ = "features_intraday"
    __table_args__ = (Index("ix_features_intraday_symbol_time", "symbol", "source_timestamp"),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(16), primary_key=True, default="1Min")
    feature_version: Mapped[str] = mapped_column(String(32), primary_key=True)

    @property
    def id(self) -> str:
        return f"{self.symbol}:{self.source_timestamp.isoformat()}:{self.timeframe}:{self.feature_version}"
    price: Mapped[float] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float)
    relative_volume: Mapped[float | None] = mapped_column(Float)
    gap_pct: Mapped[float | None] = mapped_column(Float)
    volume_spike_score: Mapped[float | None] = mapped_column(Float)
    liquidity_score: Mapped[float | None] = mapped_column(Float)
    spread_score: Mapped[float | None] = mapped_column(Float)


class FeatureDaily(TimestampMixin, Base):
    __tablename__ = "features_daily"
    __table_args__ = (Index("ix_features_daily_symbol_time", "symbol", "source_timestamp"),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    feature_version: Mapped[str] = mapped_column(String(32), primary_key=True)

    @property
    def id(self) -> str:
        return f"{self.symbol}:{self.source_timestamp.isoformat()}:{self.feature_version}"
    atr: Mapped[float | None] = mapped_column(Float)
    atr_pct: Mapped[float | None] = mapped_column(Float)
    gap_pct: Mapped[float | None] = mapped_column(Float)
    trend_score: Mapped[float | None] = mapped_column(Float)
    volatility_score: Mapped[float | None] = mapped_column(Float)
    liquidity_score: Mapped[float | None] = mapped_column(Float)


class SymbolFeatureSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "symbol_feature_snapshots"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    feature_version: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class SectorFeatureSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "sector_feature_snapshots"

    sector: Mapped[str] = mapped_column(String(128), index=True)
    feature_version: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class MarketRegimeSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "market_regime_snapshots"

    market_regime: Mapped[str] = mapped_column(String(64), default=MarketRegime.CHOPPY.value)
    confidence: Mapped[float] = mapped_column(Float)
    allowed_bias: Mapped[str] = mapped_column(String(64))
    risk_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    breakout_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    mean_reversion_permission: Mapped[str] = mapped_column(String(64), default="limited")
    reason: Mapped[str | None] = mapped_column(Text)
    hmm_state_probabilities: Mapped[dict | None] = mapped_column(JSONB)


class Event(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "events"

    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(32), default=CatalystDirection.NEUTRAL.value)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0)
    time_horizon: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)


class Catalyst(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "catalysts"

    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    catalyst_type: Mapped[str] = mapped_column(String(80), index=True)
    direction: Mapped[str] = mapped_column(String(32), default=CatalystDirection.NEUTRAL.value)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)


class EarningsCalendar(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "earnings_calendar"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    earnings_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(80))


class FilingEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "filing_events"

    raw_filing_id: Mapped[str | None] = mapped_column(ForeignKey("raw_filings.id"))
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    form_type: Mapped[str | None] = mapped_column(String(32), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)


class MacroEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "macro_events"

    event_name: Mapped[str] = mapped_column(String(160), index=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    risk_level: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(80))


class NewsCatalystScore(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "news_catalyst_scores"

    clean_news_id: Mapped[str | None] = mapped_column(ForeignKey("clean_news.id"))
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    catalyst_type: Mapped[str | None] = mapped_column(String(80))
    source_confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0)
    rumor_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_headline: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)


class StrategyRegistry(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_registry"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(
        String(40), default=StrategyStatus.RESEARCH.value, index=True
    )
    trade_type: Mapped[str] = mapped_column(String(40), default=TradeType.DAY_TRADE.value)
    allowed_timeframes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    allowed_regimes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    minimum_backtest_trades: Mapped[int] = mapped_column(Integer, default=0)
    minimum_profit_factor: Mapped[float | None] = mapped_column(Float)
    max_drawdown_limit: Mapped[float | None] = mapped_column(Float)
    allowed_symbols: Mapped[list[str]] = mapped_column(JSONB, default=list)
    max_risk_per_trade: Mapped[float | None] = mapped_column(Float)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    changed_reason: Mapped[str | None] = mapped_column(Text)
    logic_version: Mapped[str] = mapped_column(String(32), default="v1")
    target_1_rr: Mapped[float] = mapped_column(Float, default=2.0)
    target_2_rr: Mapped[float] = mapped_column(Float, default=3.0)
    minimum_trade_count_required: Mapped[int] = mapped_column(Integer, default=30)
    backtest_trade_count: Mapped[int] = mapped_column(Integer, default=0)
    out_of_sample_tested: Mapped[bool] = mapped_column(Boolean, default=False)
    walk_forward_tested: Mapped[bool] = mapped_column(Boolean, default=False)
    parameter_sensitivity_score: Mapped[float | None] = mapped_column(Float)
    paper_forward_test_days: Mapped[int | None] = mapped_column(Integer)
    evidence_quality_score: Mapped[float | None] = mapped_column(Float)


class StrategyCooldown(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_cooldowns"
    __table_args__ = (Index("ix_cooldown_symbol_strategy", "symbol", "strategy_id"),)

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ScannerResult(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "scanner_results"

    scanner_name: Mapped[str] = mapped_column(String(80), index=True)
    scanner_rule_version: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class WatchlistCandidate(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "watchlist_candidates"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CandidateHistory(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "candidate_history"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    event: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class Signal(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_signal_idempotency_key"),)

    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    trade_type: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(16), default=Direction.LONG.value)
    entry_zone: Mapped[dict] = mapped_column(JSONB)
    stop_loss: Mapped[float] = mapped_column(Float)
    target_1: Mapped[float | None] = mapped_column(Float)
    target_2: Mapped[float | None] = mapped_column(Float)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    time_horizon: Mapped[str | None] = mapped_column(String(80))
    invalidation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(40), default=SignalStatus.CANDIDATE.value, index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    signal_rule_version: Mapped[str] = mapped_column(String(32), default="v1")


class SignalVersion(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signal_versions"

    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    change_reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)


class SignalRejection(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signal_rejections"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class RiskCheck(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "risk_checks"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)
    risk_rule_version: Mapped[str] = mapped_column(String(32), default="v1", index=True)
    proposed_position_size: Mapped[float | None] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class RiskRejection(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "risk_rejections"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    risk_rule_version: Mapped[str] = mapped_column(String(32), default="v1")
    payload: Mapped[dict | None] = mapped_column(JSONB)


class KillSwitchEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "kill_switch_events"

    event_type: Mapped[str] = mapped_column(String(80), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_reason: Mapped[str | None] = mapped_column(Text)


class ExposureSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "exposure_snapshots"

    account_equity: Mapped[float] = mapped_column(Float)
    total_exposure: Mapped[float] = mapped_column(Float)
    sector_exposure: Mapped[dict] = mapped_column(JSONB, default=dict)
    strategy_exposure: Mapped[dict] = mapped_column(JSONB, default=dict)
    symbol_exposure: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)


class BrokerAccountSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "broker_account_snapshots"

    environment_mode: Mapped[str] = mapped_column(
        String(32), default=EnvironmentMode.PAPER.value, index=True
    )
    broker: Mapped[str] = mapped_column(String(80), default="alpaca_paper", index=True)
    account_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(16))
    equity: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    buying_power: Mapped[float | None] = mapped_column(Float)
    daytrade_count: Mapped[int | None] = mapped_column(Integer)
    pattern_day_trader: Mapped[bool | None] = mapped_column(Boolean)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)


class Order(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_order_idempotency_key"),)

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value)
    execution_environment: Mapped[str] = mapped_column(
        String(32), default=ExecutionEnvironment.PAPER.value, index=True
    )
    broker: Mapped[str] = mapped_column(String(80), default="alpaca_paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(32), default="limit")
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40), default=OrderStatus.CREATED.value, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    expected_price: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Fill(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("broker_fill_id", name="uq_fill_broker_fill_id"),)

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float | None] = mapped_column(Float)


class Position(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("environment_mode", "symbol", name="uq_position_mode_symbol"),
    )

    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_price: Mapped[float | None] = mapped_column(Float)
    broker_quantity: Mapped[float | None] = mapped_column(Float)
    broker_average_price: Mapped[float | None] = mapped_column(Float)
    reconciliation_status: Mapped[str] = mapped_column(String(40), default="UNKNOWN")
    reason: Mapped[str | None] = mapped_column(Text)


class SystemLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "system_logs"
    __table_args__ = (
        Index("ix_system_logs_type_time", "log_type", "source_timestamp"),
        Index("ix_system_logs_entity", "entity_type", "entity_id"),
        Index("ix_system_logs_payload_gin", "payload", postgresql_using="gin"),
    )

    log_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), index=True)
    actor: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str | None] = mapped_column(String(40), index=True)
    severity: Mapped[str | None] = mapped_column(String(20), index=True)
    success: Mapped[bool | None] = mapped_column(Boolean, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class TradeJournal(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "trade_journal"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    entry_thesis: Mapped[str | None] = mapped_column(Text)
    actual_entry: Mapped[float | None] = mapped_column(Float)
    actual_exit: Mapped[float | None] = mapped_column(Float)
    market_regime: Mapped[str | None] = mapped_column(String(64))
    catalyst: Mapped[str | None] = mapped_column(Text)
    pnl: Mapped[float | None] = mapped_column(Float)
    max_favorable_excursion: Mapped[float | None] = mapped_column(Float)
    max_adverse_excursion: Mapped[float | None] = mapped_column(Float)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    time_in_trade_seconds: Mapped[float | None] = mapped_column(Float)
    rule_violations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    mistake_tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    ai_review: Mapped[str | None] = mapped_column(Text)
    human_notes: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)


class AuditLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_payload_gin", "payload", postgresql_using="gin"),
    )

    actor: Mapped[str] = mapped_column(String(80), default="system")
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class DecisionLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "decision_logs"
    __table_args__ = (
        Index("ix_decision_logs_payload_gin", "payload", postgresql_using="gin"),
    )

    decision_type: Mapped[str] = mapped_column(
        String(40), default=DecisionType.SCANNER.value, index=True
    )
    outcome: Mapped[str] = mapped_column(
        String(40), default=DecisionOutcome.RECORDED.value, index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    rule_version: Mapped[str | None] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class WeeklyReview(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "weekly_reviews"

    week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    week_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)


class BacktestReport(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "backtest_reports"

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="v1", index=True)
    universe_name: Mapped[str | None] = mapped_column(String(120), index=True)
    assumptions: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    report_uri: Mapped[str | None] = mapped_column(Text)
    survivorship_bias_warning: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


class OpportunityScore(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "opportunity_scores"

    scanner_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("scanner_results.id"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    setup_type: Mapped[str | None] = mapped_column(String(80), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    grade: Mapped[str] = mapped_column(String(16), index=True)
    component_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    penalties: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    explanation: Mapped[str] = mapped_column(Text)
    expected_r: Mapped[float | None] = mapped_column(Float)
    historical_win_rate: Mapped[float | None] = mapped_column(Float)
    expectancy_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    confidence_level: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_risk_multiplier: Mapped[float] = mapped_column(Float, default=0.0)
    market_regime: Mapped[str | None] = mapped_column(String(64), index=True)
    sector_regime: Mapped[str | None] = mapped_column(String(64), index=True)
    catalyst_type: Mapped[str | None] = mapped_column(String(80), index=True)
    linked_news_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class OpportunityScoreComponent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "opportunity_score_components"

    opportunity_score_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_scores.id"), index=True
    )
    component_name: Mapped[str] = mapped_column(String(80), index=True)
    raw_value: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[str | None] = mapped_column(Text)


class ExpectancySnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "expectancy_snapshots"

    bucket_type: Mapped[str] = mapped_column(String(80), index=True)
    bucket_key: Mapped[str] = mapped_column(String(160), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    setup_type: Mapped[str | None] = mapped_column(String(80), index=True)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float)
    average_win: Mapped[float | None] = mapped_column(Float)
    average_loss: Mapped[float | None] = mapped_column(Float)
    expectancy_r: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)
    average_hold_seconds: Mapped[float | None] = mapped_column(Float)
    average_slippage_bps: Mapped[float | None] = mapped_column(Float)
    average_mfe: Mapped[float | None] = mapped_column(Float)
    average_mae: Mapped[float | None] = mapped_column(Float)
    confidence_level: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class StrategyPerformanceBucket(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_performance_buckets"

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    setup_type: Mapped[str | None] = mapped_column(String(80), index=True)
    bucket_type: Mapped[str] = mapped_column(String(80), index=True)
    bucket_key: Mapped[str] = mapped_column(String(160), index=True)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    expectancy_r: Mapped[float | None] = mapped_column(Float)
    win_rate: Mapped[float | None] = mapped_column(Float)
    recent_expectancy_r: Mapped[float | None] = mapped_column(Float)
    decay_warning: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confidence_level: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class SectorStrengthSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "sector_strength_snapshots"

    sector: Mapped[str] = mapped_column(String(128), index=True)
    sector_etf: Mapped[str | None] = mapped_column(String(16), index=True)
    sector_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    sector_vs_spy_score: Mapped[float | None] = mapped_column(Float)
    breadth_score: Mapped[float | None] = mapped_column(Float)
    regime: Mapped[str | None] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class SymbolRelativeStrengthSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "symbol_relative_strength_snapshots"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    sector: Mapped[str | None] = mapped_column(String(128), index=True)
    sector_etf: Mapped[str | None] = mapped_column(String(16), index=True)
    stock_vs_spy_score: Mapped[float | None] = mapped_column(Float)
    stock_vs_sector_score: Mapped[float | None] = mapped_column(Float)
    leadership_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    candidate_reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class PointInTimeUniverseMembership(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "point_in_time_universe_memberships"
    __table_args__ = (
        UniqueConstraint(
            "universe_name",
            "as_of_date",
            "symbol",
            name="uq_pit_universe_membership_asof_symbol",
        ),
        Index("ix_pit_universe_asof_active", "universe_name", "as_of_date", "is_active"),
    )

    universe_name: Mapped[str] = mapped_column(String(80), index=True)
    as_of_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    asset_class: Mapped[str | None] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(32))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_tradable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_liquid: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    membership_reason: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class AlphaRejectionReason(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "alpha_rejection_reasons"

    scanner_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("scanner_results.id"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    setup_type: Mapped[str | None] = mapped_column(String(80), index=True)
    reason_code: Mapped[str] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(32), default="BLOCKER", index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class ShortInterestSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "short_interest_snapshots"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    short_interest_pct_float: Mapped[float | None] = mapped_column(Float)
    days_to_cover: Mapped[float | None] = mapped_column(Float)
    borrow_fee_pct: Mapped[float | None] = mapped_column(Float)
    utilization_pct: Mapped[float | None] = mapped_column(Float)
    float_shares: Mapped[float | None] = mapped_column(Float)
    short_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    data_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)


class OptionsIntelligenceSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "options_intelligence_snapshots"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    iv_rank: Mapped[float | None] = mapped_column(Float)
    iv_percentile: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    gamma_exposure: Mapped[float | None] = mapped_column(Float)
    delta_exposure: Mapped[float | None] = mapped_column(Float)
    expected_move_pct: Mapped[float | None] = mapped_column(Float)
    options_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    weekly_expiry: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    earnings_expiry: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    data_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)


class StrategySetupTag(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_setup_tags"
    __table_args__ = (
        UniqueConstraint("strategy_id", "setup_type", "tag", name="uq_strategy_setup_tag"),
    )

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    setup_type: Mapped[str] = mapped_column(String(80), index=True)
    tag: Mapped[str] = mapped_column(String(80), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class StrategyRecommendation(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_recommendations"

    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    recommendation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=RecommendationStatus.PROPOSED.value)
    reason: Mapped[str] = mapped_column(Text)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class ParameterChangeRequest(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "parameter_change_requests"

    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    parameter_name: Mapped[str] = mapped_column(String(128))
    current_value: Mapped[str | None] = mapped_column(Text)
    proposed_value: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=RecommendationStatus.PROPOSED.value)
    reason: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_reason: Mapped[str | None] = mapped_column(Text)
