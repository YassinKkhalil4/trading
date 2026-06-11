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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_system.app.core.enums import (
    AdminRole,
    CatalystDirection,
    DataQualityStatus,
    DecisionOutcome,
    DecisionType,
    Direction,
    EnvironmentMode,
    ExecutionEnvironment,
    LiveApprovalStatus,
    MarketRegime,
    OrderStatus,
    ProviderReliabilityLevel,
    RecommendationStatus,
    SignalStatus,
    StrategyApprovalStatus,
    StrategyStatus,
    TradeType,
)
from trading_system.app.db.base import Base


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
    payload: Mapped[dict | None] = mapped_column(JSON)


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
    raw_asset_payload: Mapped[dict | None] = mapped_column(JSON)
    tradability_reason: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)


class RawMarketData(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_market_data"
    __table_args__ = (
        UniqueConstraint("provider", "symbol", "timeframe", "source_timestamp", name="uq_raw_candle"),
        Index("ix_raw_market_data_symbol_time", "symbol", "source_timestamp"),
    )

    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RawTradeTick(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_trade_ticks"
    __table_args__ = (
        UniqueConstraint("provider", "symbol", "trade_id", name="uq_raw_trade_tick_provider_trade"),
        Index("ix_raw_trade_ticks_symbol_time", "symbol", "source_timestamp"),
    )

    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(128), index=True)
    price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    exchange: Mapped[str | None] = mapped_column(String(64))
    conditions: Mapped[list[str] | None] = mapped_column(JSON)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RawIngestionEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_ingestion_events"
    __table_args__ = (
        Index("ix_raw_ingestion_events_type_status", "payload_type", "status"),
        Index("ix_raw_ingestion_events_provider_time", "provider", "source_timestamp"),
    )

    payload_type: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PROCESSED", index=True)
    raw_table: Mapped[str] = mapped_column(String(80))
    raw_row_id: Mapped[str] = mapped_column(String(36), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class CleanMarketData(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "clean_market_data"
    __table_args__ = (
        UniqueConstraint(
            "provider", "symbol", "timeframe", "source_timestamp", name="uq_clean_candle"
        ),
        Index("ix_clean_market_data_symbol_time", "symbol", "source_timestamp"),
    )

    raw_market_data_id: Mapped[str | None] = mapped_column(ForeignKey("raw_market_data.id"))
    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
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
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RawNews(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "raw_news"

    provider: Mapped[str] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
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

    provider: Mapped[str] = mapped_column(String(80), default="sec_edgar", index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    form_type: Mapped[str | None] = mapped_column(String(32), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
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
    payload: Mapped[dict | None] = mapped_column(JSON)


class WorkerHeartbeat(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "worker_heartbeats"

    worker_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class DataQualityError(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "data_quality_errors"

    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(16))
    data_quality_status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


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


class FeatureIntraday(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "features_intraday"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), default="1Min")
    feature_version: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float)
    relative_volume: Mapped[float | None] = mapped_column(Float)
    gap_pct: Mapped[float | None] = mapped_column(Float)
    volume_spike_score: Mapped[float | None] = mapped_column(Float)
    liquidity_score: Mapped[float | None] = mapped_column(Float)
    spread_score: Mapped[float | None] = mapped_column(Float)


class FeatureDaily(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "features_daily"

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    feature_version: Mapped[str] = mapped_column(String(32), index=True)
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
    snapshot: Mapped[dict] = mapped_column(JSON)


class SectorFeatureSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "sector_feature_snapshots"

    sector: Mapped[str] = mapped_column(String(128), index=True)
    feature_version: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)


class MarketRegimeSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "market_regime_snapshots"

    market_regime: Mapped[str] = mapped_column(String(64), default=MarketRegime.CHOPPY.value)
    confidence: Mapped[float] = mapped_column(Float)
    allowed_bias: Mapped[str] = mapped_column(String(64))
    risk_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    breakout_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    mean_reversion_permission: Mapped[str] = mapped_column(String(64), default="limited")
    reason: Mapped[str | None] = mapped_column(Text)


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
    status: Mapped[str] = mapped_column(String(40), default=StrategyStatus.RESEARCH.value, index=True)
    trade_type: Mapped[str] = mapped_column(String(40), default=TradeType.DAY_TRADE.value)
    allowed_timeframes: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_regimes: Mapped[list[str]] = mapped_column(JSON, default=list)
    minimum_backtest_trades: Mapped[int] = mapped_column(Integer, default=0)
    minimum_profit_factor: Mapped[float | None] = mapped_column(Float)
    max_drawdown_limit: Mapped[float | None] = mapped_column(Float)
    allowed_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_risk_per_trade: Mapped[float | None] = mapped_column(Float)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    changed_reason: Mapped[str | None] = mapped_column(Text)
    logic_version: Mapped[str] = mapped_column(String(32), default="v1")
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


class StrategyApprovalRequest(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "strategy_approval_requests"

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="v1", index=True)
    requested_status: Mapped[str] = mapped_column(String(40), index=True)
    current_status: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(32), default=StrategyApprovalStatus.REQUESTED.value, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(80), default="system")
    approved_by: Mapped[str | None] = mapped_column(String(80))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScannerResult(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "scanner_results"

    scanner_name: Mapped[str] = mapped_column(String(80), index=True)
    scanner_rule_version: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


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
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Signal(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_signal_idempotency_key"),)

    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    trade_type: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(16), default=Direction.LONG.value)
    entry_zone: Mapped[dict] = mapped_column(JSON)
    stop_loss: Mapped[float] = mapped_column(Float)
    target_1: Mapped[float | None] = mapped_column(Float)
    target_2: Mapped[float | None] = mapped_column(Float)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    time_horizon: Mapped[str | None] = mapped_column(String(80))
    invalidation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default=SignalStatus.CANDIDATE.value, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    signal_rule_version: Mapped[str] = mapped_column(String(32), default="v1")


class SignalVersion(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signal_versions"

    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    change_reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)


class SignalRejection(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "signal_rejections"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class TradeThesis(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "trade_theses"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    prompt_version: Mapped[str] = mapped_column(String(32), default="ai_thesis_prompt_v1")
    trade_type: Mapped[str] = mapped_column(String(40))
    setup_quality: Mapped[float] = mapped_column(Float)
    catalyst_quality: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    reason_for_trade: Mapped[str] = mapped_column(Text)
    invalidation_reason: Mapped[str] = mapped_column(Text)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    suggested_holding_period: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)


class RiskCheck(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "risk_checks"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)
    risk_rule_version: Mapped[str] = mapped_column(String(32), default="v1", index=True)
    proposed_position_size: Mapped[float | None] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskRejection(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "risk_rejections"

    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    risk_rule_version: Mapped[str] = mapped_column(String(32), default="v1")
    payload: Mapped[dict | None] = mapped_column(JSON)


class KillSwitchEvent(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "kill_switch_events"

    event_type: Mapped[str] = mapped_column(String(80), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_reason: Mapped[str | None] = mapped_column(Text)


class ExposureSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "exposure_snapshots"

    account_equity: Mapped[float] = mapped_column(Float)
    total_exposure: Mapped[float] = mapped_column(Float)
    sector_exposure: Mapped[dict] = mapped_column(JSON, default=dict)
    strategy_exposure: Mapped[dict] = mapped_column(JSON, default=dict)
    symbol_exposure: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)


class BrokerAccountSnapshot(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "broker_account_snapshots"

    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value, index=True)
    broker: Mapped[str] = mapped_column(String(80), default="alpaca_paper", index=True)
    account_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(16))
    equity: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    buying_power: Mapped[float | None] = mapped_column(Float)
    daytrade_count: Mapped[int | None] = mapped_column(Integer)
    pattern_day_trader: Mapped[bool | None] = mapped_column(Boolean)
    payload: Mapped[dict | None] = mapped_column(JSON)
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
    __table_args__ = (UniqueConstraint("environment_mode", "symbol", name="uq_position_mode_symbol"),)

    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_price: Mapped[float | None] = mapped_column(Float)
    broker_quantity: Mapped[float | None] = mapped_column(Float)
    broker_average_price: Mapped[float | None] = mapped_column(Float)
    reconciliation_status: Mapped[str] = mapped_column(String(40), default="UNKNOWN")
    reason: Mapped[str | None] = mapped_column(Text)


class BrokerSyncLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "broker_sync_logs"

    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value)
    broker: Mapped[str] = mapped_column(String(80), default="alpaca_paper")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    mismatch_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class ExecutionError(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "execution_errors"

    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    environment_mode: Mapped[str] = mapped_column(String(32), default=EnvironmentMode.PAPER.value)
    error_type: Mapped[str] = mapped_column(String(80), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class LiveReadinessCheck(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "live_readiness_checks"

    check_name: Mapped[str] = mapped_column(String(120), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), default="BLOCKER", index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class LiveReadinessReport(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "live_readiness_reports"

    overall_status: Mapped[str] = mapped_column(String(40), index=True)
    live_allowed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(Text)
    checks: Mapped[list[dict]] = mapped_column(JSON, default=list)


class LiveTradingApproval(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "live_trading_approvals"

    status: Mapped[str] = mapped_column(String(32), default=LiveApprovalStatus.ACTIVE.value, index=True)
    approved_by: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)


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
    rule_violations: Mapped[list[str]] = mapped_column(JSON, default=list)
    mistake_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    ai_review: Mapped[str | None] = mapped_column(Text)
    human_notes: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)


class AuditLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(80), default="system")
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class DecisionLog(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "decision_logs"

    decision_type: Mapped[str] = mapped_column(String(40), default=DecisionType.SCANNER.value, index=True)
    outcome: Mapped[str] = mapped_column(String(40), default=DecisionOutcome.RECORDED.value, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(80), index=True)
    rule_version: Mapped[str | None] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class AIPromptTemplate(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "ai_prompt_templates"
    __table_args__ = (UniqueConstraint("template_name", "version", name="uq_prompt_template_version"),)

    template_name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(32), index=True)
    prompt_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    change_reason: Mapped[str | None] = mapped_column(Text)


class AIReview(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "ai_reviews"

    trade_journal_id: Mapped[str | None] = mapped_column(ForeignKey("trade_journal.id"), index=True)
    prompt_template_id: Mapped[str | None] = mapped_column(ForeignKey("ai_prompt_templates.id"))
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1")
    review_text: Mapped[str] = mapped_column(Text)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)


class WeeklyReview(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "weekly_reviews"

    week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    week_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)


class BacktestReport(IdMixin, TimestampMixin, SourceTimestampMixin, Base):
    __tablename__ = "backtest_reports"

    strategy_id: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="v1", index=True)
    universe_name: Mapped[str | None] = mapped_column(String(120), index=True)
    assumptions: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    report_uri: Mapped[str | None] = mapped_column(Text)
    survivorship_bias_warning: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


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
    human_approved_by: Mapped[str | None] = mapped_column(String(80))
    human_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_reason: Mapped[str | None] = mapped_column(Text)
