from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from trading_system.app.core.enums import EnvironmentMode


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    environment_mode: EnvironmentMode = EnvironmentMode.RESEARCH
    database_url: str = "sqlite:///trading_system.db"
    app_name: str = "Autonomous Trading Intelligence Platform"
    deployment_target: str = "local"
    aws_region: str = "us-east-1"
    redis_url: str = "redis://localhost:6379/0"
    raw_archive_bucket: str = ""

    alpaca_paper_api_key: str = ""
    alpaca_paper_secret_key: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_paper_data_url: str = "https://data.alpaca.markets"
    alpaca_market_data_stream_url: str = "wss://stream.data.alpaca.markets/v2/iex"
    alpaca_market_data_feed: str = "iex"
    alpaca_primary_data_feed: str = "iex"
    alpaca_stream_symbols: str = "SPY,QQQ,AMD,NVDA,TSLA"
    alpaca_stream_channels: str = "bars,quotes,trades,statuses"
    alpaca_stream_max_reconnects: int = 10
    alpaca_stream_max_messages: int = 0
    alpaca_bars_timeframe: str = "1Min"
    alpaca_bars_limit: int = 1000
    alpaca_order_max_attempts: int = 3
    alpaca_order_retry_backoff_seconds: float = 0.0

    # Live config is code-capable, but disabled by default and gated at runtime.
    alpaca_live_api_key: str = ""
    alpaca_live_secret_key: str = ""
    alpaca_live_base_url: str = "https://api.alpaca.markets"
    alpaca_live_data_url: str = "https://data.alpaca.markets"
    allow_live_trading: bool = False
    confirm_live_trading: str = ""
    enable_live_order_path: bool = False
    live_readiness_max_age_minutes: int = 60
    live_approval_max_age_minutes: int = 240

    admin_username: str = "admin"
    admin_password: str = ""
    admin_session_secret: str = "change-me"
    api_admin_token: str = ""
    auth_session_minutes: int = 480
    admin_failed_login_lockout_attempts: int = 5
    admin_lockout_minutes: int = 15

    risk_per_trade_pct: float = 0.25
    max_daily_loss_pct: float = 1.0
    max_weekly_loss_pct: float = 3.0
    max_open_positions: int = 3
    max_single_sector_exposure_pct: float = 30.0
    max_symbol_exposure_pct: float = 20.0
    max_strategy_exposure_pct: float = 40.0
    max_correlated_exposure_pct: float = 50.0
    max_overnight_exposure_pct: float = 50.0
    min_cash_buffer_pct: float = 10.0
    max_same_sector_new_signals: int = 2
    max_trades_per_day: int = 5
    max_trades_per_strategy_per_day: int = 2
    max_slippage_bps: float = 25.0
    max_volatility_score: float = 95.0
    max_order_stale_seconds: int = 120
    quote_freshness_max_seconds: int = 5
    bar_freshness_max_seconds: int = 90

    min_price: float = 5.0
    min_average_volume: int = 1_000_000
    min_dollar_volume: float = 20_000_000.0
    max_spread_bps: float = 20.0
    dashboard_refresh_seconds: int = 15
    news_rss_feeds: str = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    alpha_vantage_api_key: str = ""
    alpha_vantage_news_limit: int = 1000
    sec_user_agent: str = "AutonomousTradingIntelligence contact@example.com"
    sec_requests_per_second: float = 5.0
    scheduler_market_data_seconds: int = 60
    scheduler_fill_reconciliation_seconds: int = 30
    scheduler_sec_seconds: int = 3600
    scheduler_news_seconds: int = 300
    scheduler_news_premarket: bool = True
    scheduler_news_intraday_pulls: int = 13
    scheduler_regime_seconds: int = 60
    scheduler_catalyst_seconds: int = 300
    scheduler_trade_monitor_seconds: int = 15
    scheduler_review_seconds: int = 3600
    provider_health_max_age_seconds: int = 180
    worker_sleep_seconds: int = 5
    scheduler_lock_ttl_seconds: int = 300
    scheduler_use_master_universe_refresh: bool = True
    # News-only mode: scan ALL US stocks & ETFs purely from Alpha Vantage news.
    # When True the scheduler skips every price/market-data job (market data,
    # features, price scanners, candle repair, regime, SEC) and the universe
    # refresh activates all tradable assets without a price-based liquidity cap.
    news_only_mode: bool = True
    scheduler_news_screener_seconds: int = 300

    # Opportunity ranking engine. The score is a weighted average of the eight
    # components below, so it always lands on a 0-100 scale regardless of the
    # configured weights. Grade thresholds are evaluated against that 0-100 score.
    #
    # enable_ranking_signal_path routes accepted live VWAP scans through the
    # opportunity-ranking bridge (only A/A+ grades create signals). Keep it OFF
    # unless the provider-health AND market-regime schedulers are running: the
    # ranking hard-block requires a fresh Alpaca provider-health snapshot and a
    # fresh regime snapshot, so with the flag ON and those schedulers idle the
    # scan will accept candidates but never create a signal.
    enable_ranking_signal_path: bool = False
    # Weights are tuned toward components that predict follow-through (scanner
    # conviction, regime, relative strength, catalyst). Provider reliability and
    # data freshness act mostly as hard-block GATES (see _hard_block_reason), so
    # they intentionally carry low ranking weight rather than driving the score.
    ranking_weight_scanner: float = 25.0
    ranking_weight_freshness: float = 10.0
    ranking_weight_provider: float = 5.0
    ranking_weight_regime: float = 15.0
    ranking_weight_catalyst: float = 15.0
    ranking_weight_relative_strength: float = 15.0
    ranking_weight_liquidity: float = 10.0
    ranking_weight_spread: float = 5.0
    ranking_grade_a_plus_min: float = 88.0
    ranking_grade_a_min: float = 78.0
    ranking_grade_b_min: float = 65.0
    ranking_grade_watch_min: float = 50.0
    ranking_relative_strength_multiplier: float = 20.0
    ranking_neutral_component_score: float = 50.0
    # Provider reported HEALTHY but no numeric reliability: treat as good-but-not-
    # perfect rather than a free 100, so an unknown provider can't inflate scores.
    ranking_unknown_provider_reliability: float = 75.0

    @property
    def live_order_path_enabled(self) -> bool:
        return (
            self.enable_live_order_path
            and self.environment_mode == EnvironmentMode.LIVE
            and self.allow_live_trading
            and self.confirm_live_trading == "I_UNDERSTAND_RISK"
            and bool(self.alpaca_live_api_key)
            and bool(self.alpaca_live_secret_key)
        )

    @property
    def auto_create_schema_enabled(self) -> bool:
        return self.deployment_target == "local" and self.database_url.startswith("sqlite")

    def require_research_or_paper(self) -> None:
        if self.environment_mode == EnvironmentMode.LIVE:
            raise RuntimeError("This operation is only allowed in research or paper mode.")

    def require_paper_mode(self) -> None:
        if self.environment_mode != EnvironmentMode.PAPER:
            raise RuntimeError("Paper execution requires ENVIRONMENT_MODE=paper.")

    def require_live_disabled(self) -> None:
        if self.live_order_path_enabled:
            raise RuntimeError("This operation requires the live order path to be disabled.")


@lru_cache
def get_settings() -> Settings:
    mode = EnvironmentMode(os.getenv("ENVIRONMENT_MODE", EnvironmentMode.RESEARCH.value))
    if mode == EnvironmentMode.LIVE:
        allow_live = _env_bool("ALLOW_LIVE_TRADING", False)
        confirmation = os.getenv("CONFIRM_LIVE_TRADING", "")
        if not allow_live or confirmation != "I_UNDERSTAND_RISK":
            raise RuntimeError(
                "ENVIRONMENT_MODE=live requires explicit live confirmation. "
                "Live trading is not wired unless every live gate is explicitly enabled."
            )

    return Settings(
        environment_mode=mode,
        database_url=os.getenv(
            "DATABASE_URL",
            "sqlite:///trading_system.db",
        ),
        app_name=os.getenv("APP_NAME", "Autonomous Trading Intelligence Platform"),
        deployment_target=os.getenv("DEPLOYMENT_TARGET", "local"),
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        raw_archive_bucket=os.getenv("RAW_ARCHIVE_BUCKET", ""),
        alpaca_paper_api_key=os.getenv("ALPACA_PAPER_API_KEY", ""),
        alpaca_paper_secret_key=os.getenv("ALPACA_PAPER_SECRET_KEY", ""),
        alpaca_paper_base_url=os.getenv(
            "ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets"
        ),
        alpaca_paper_data_url=os.getenv("ALPACA_PAPER_DATA_URL", "https://data.alpaca.markets"),
        alpaca_market_data_stream_url=os.getenv(
            "ALPACA_MARKET_DATA_STREAM_URL", "wss://stream.data.alpaca.markets/v2/iex"
        ),
        alpaca_market_data_feed=os.getenv("ALPACA_MARKET_DATA_FEED", "iex"),
        alpaca_primary_data_feed=os.getenv("ALPACA_PRIMARY_DATA_FEED", "iex"),
        alpaca_stream_symbols=os.getenv("ALPACA_STREAM_SYMBOLS", "SPY,QQQ,AMD,NVDA,TSLA"),
        alpaca_stream_channels=os.getenv("ALPACA_STREAM_CHANNELS", "bars,quotes,trades,statuses"),
        alpaca_stream_max_reconnects=_env_int("ALPACA_STREAM_MAX_RECONNECTS", 10),
        alpaca_stream_max_messages=_env_int("ALPACA_STREAM_MAX_MESSAGES", 0),
        alpaca_bars_timeframe=os.getenv("ALPACA_BARS_TIMEFRAME", "1Min"),
        alpaca_bars_limit=_env_int("ALPACA_BARS_LIMIT", 1000),
        alpaca_order_max_attempts=_env_int("ALPACA_ORDER_MAX_ATTEMPTS", 3),
        alpaca_order_retry_backoff_seconds=_env_float("ALPACA_ORDER_RETRY_BACKOFF_SECONDS", 0.0),
        alpaca_live_api_key=os.getenv("ALPACA_LIVE_API_KEY", ""),
        alpaca_live_secret_key=os.getenv("ALPACA_LIVE_SECRET_KEY", ""),
        alpaca_live_base_url=os.getenv("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets"),
        alpaca_live_data_url=os.getenv("ALPACA_LIVE_DATA_URL", "https://data.alpaca.markets"),
        allow_live_trading=_env_bool("ALLOW_LIVE_TRADING", False),
        confirm_live_trading=os.getenv("CONFIRM_LIVE_TRADING", ""),
        enable_live_order_path=_env_bool("ENABLE_LIVE_ORDER_PATH", False),
        live_readiness_max_age_minutes=_env_int("LIVE_READINESS_MAX_AGE_MINUTES", 60),
        live_approval_max_age_minutes=_env_int("LIVE_APPROVAL_MAX_AGE_MINUTES", 240),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        admin_session_secret=os.getenv("ADMIN_SESSION_SECRET", "change-me"),
        api_admin_token=os.getenv("API_ADMIN_TOKEN", ""),
        auth_session_minutes=_env_int("AUTH_SESSION_MINUTES", 480),
        admin_failed_login_lockout_attempts=_env_int("ADMIN_FAILED_LOGIN_LOCKOUT_ATTEMPTS", 5),
        admin_lockout_minutes=_env_int("ADMIN_LOCKOUT_MINUTES", 15),
        risk_per_trade_pct=_env_float("RISK_PER_TRADE_PCT", 0.25),
        max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", 1.0),
        max_weekly_loss_pct=_env_float("MAX_WEEKLY_LOSS_PCT", 3.0),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
        max_single_sector_exposure_pct=_env_float("MAX_SINGLE_SECTOR_EXPOSURE_PCT", 30.0),
        max_symbol_exposure_pct=_env_float("MAX_SYMBOL_EXPOSURE_PCT", 20.0),
        max_strategy_exposure_pct=_env_float("MAX_STRATEGY_EXPOSURE_PCT", 40.0),
        max_correlated_exposure_pct=_env_float("MAX_CORRELATED_EXPOSURE_PCT", 50.0),
        max_overnight_exposure_pct=_env_float("MAX_OVERNIGHT_EXPOSURE_PCT", 50.0),
        min_cash_buffer_pct=_env_float("MIN_CASH_BUFFER_PCT", 10.0),
        max_same_sector_new_signals=_env_int("MAX_SAME_SECTOR_NEW_SIGNALS", 2),
        max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 5),
        max_trades_per_strategy_per_day=_env_int("MAX_TRADES_PER_STRATEGY_PER_DAY", 2),
        max_slippage_bps=_env_float("MAX_SLIPPAGE_BPS", 25.0),
        max_volatility_score=_env_float("MAX_VOLATILITY_SCORE", 95.0),
        max_order_stale_seconds=_env_int("MAX_ORDER_STALE_SECONDS", 120),
        quote_freshness_max_seconds=_env_int("QUOTE_FRESHNESS_MAX_SECONDS", 5),
        bar_freshness_max_seconds=_env_int("BAR_FRESHNESS_MAX_SECONDS", 90),
        min_price=_env_float("MIN_PRICE", 5.0),
        min_average_volume=_env_int("MIN_AVERAGE_VOLUME", 1_000_000),
        min_dollar_volume=_env_float("MIN_DOLLAR_VOLUME", 20_000_000.0),
        max_spread_bps=_env_float("MAX_SPREAD_BPS", 20.0),
        dashboard_refresh_seconds=_env_int("DASHBOARD_REFRESH_SECONDS", 15),
        news_rss_feeds=os.getenv(
            "NEWS_RSS_FEEDS",
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
        ),
        alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY", ""),
        alpha_vantage_news_limit=_env_int("ALPHA_VANTAGE_NEWS_LIMIT", 1000),
        sec_user_agent=os.getenv("SEC_USER_AGENT", "AutonomousTradingIntelligence contact@example.com"),
        sec_requests_per_second=_env_float("SEC_REQUESTS_PER_SECOND", 5.0),
        scheduler_market_data_seconds=_env_int("SCHEDULER_MARKET_DATA_SECONDS", 60),
        scheduler_fill_reconciliation_seconds=_env_int("SCHEDULER_FILL_RECONCILIATION_SECONDS", 30),
        scheduler_sec_seconds=_env_int("SCHEDULER_SEC_SECONDS", 3600),
        scheduler_news_seconds=_env_int("SCHEDULER_NEWS_SECONDS", 300),
        scheduler_news_premarket=_env_bool("SCHEDULER_NEWS_PREMARKET", True),
        scheduler_news_intraday_pulls=_env_int("SCHEDULER_NEWS_INTRADAY_PULLS", 13),
        scheduler_regime_seconds=_env_int("SCHEDULER_REGIME_SECONDS", 60),
        scheduler_catalyst_seconds=_env_int("SCHEDULER_CATALYST_SECONDS", 300),
        scheduler_trade_monitor_seconds=_env_int("SCHEDULER_TRADE_MONITOR_SECONDS", 15),
        scheduler_review_seconds=_env_int("SCHEDULER_REVIEW_SECONDS", 3600),
        provider_health_max_age_seconds=_env_int("PROVIDER_HEALTH_MAX_AGE_SECONDS", 180),
        worker_sleep_seconds=_env_int("WORKER_SLEEP_SECONDS", 5),
        scheduler_lock_ttl_seconds=_env_int("SCHEDULER_LOCK_TTL_SECONDS", 300),
        scheduler_use_master_universe_refresh=_env_bool("SCHEDULER_USE_MASTER_UNIVERSE_REFRESH", True),
        news_only_mode=_env_bool("NEWS_ONLY_MODE", True),
        scheduler_news_screener_seconds=_env_int("SCHEDULER_NEWS_SCREENER_SECONDS", 300),
        enable_ranking_signal_path=_env_bool("ENABLE_RANKING_SIGNAL_PATH", False),
        ranking_weight_scanner=_env_float("RANKING_WEIGHT_SCANNER", 25.0),
        ranking_weight_freshness=_env_float("RANKING_WEIGHT_FRESHNESS", 10.0),
        ranking_weight_provider=_env_float("RANKING_WEIGHT_PROVIDER", 5.0),
        ranking_weight_regime=_env_float("RANKING_WEIGHT_REGIME", 15.0),
        ranking_weight_catalyst=_env_float("RANKING_WEIGHT_CATALYST", 15.0),
        ranking_weight_relative_strength=_env_float("RANKING_WEIGHT_RELATIVE_STRENGTH", 15.0),
        ranking_weight_liquidity=_env_float("RANKING_WEIGHT_LIQUIDITY", 10.0),
        ranking_weight_spread=_env_float("RANKING_WEIGHT_SPREAD", 5.0),
        ranking_grade_a_plus_min=_env_float("RANKING_GRADE_A_PLUS_MIN", 88.0),
        ranking_grade_a_min=_env_float("RANKING_GRADE_A_MIN", 78.0),
        ranking_grade_b_min=_env_float("RANKING_GRADE_B_MIN", 65.0),
        ranking_grade_watch_min=_env_float("RANKING_GRADE_WATCH_MIN", 50.0),
        ranking_relative_strength_multiplier=_env_float("RANKING_RELATIVE_STRENGTH_MULTIPLIER", 20.0),
        ranking_neutral_component_score=_env_float("RANKING_NEUTRAL_COMPONENT_SCORE", 50.0),
        ranking_unknown_provider_reliability=_env_float("RANKING_UNKNOWN_PROVIDER_RELIABILITY", 75.0),
    )
