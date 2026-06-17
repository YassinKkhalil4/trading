from __future__ import annotations

from enum import Enum


class EnvironmentMode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE_DISABLED = "live_disabled"
    LIVE = "live"


class ExecutionEnvironment(str, Enum):
    PAPER = "PAPER"
    LIVE_DISABLED = "LIVE_DISABLED"
    LIVE = "LIVE"


class AdminRole(str, Enum):
    ADMIN = "ADMIN"
    TRADER = "TRADER"
    VIEWER = "VIEWER"


class ProviderHealthStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DOWN = "DOWN"


class DataQualityStatus(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    DUPLICATE = "DUPLICATE"
    SUSPICIOUS_PRICE = "SUSPICIOUS_PRICE"
    SUSPICIOUS_VOLUME = "SUSPICIOUS_VOLUME"
    STALE = "STALE"
    OUT_OF_SESSION = "OUT_OF_SESSION"


class StrategyStatus(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER_TESTING = "PAPER_TESTING"
    APPROVED_SMALL_SIZE = "APPROVED_SMALL_SIZE"
    APPROVED_FULL_SIZE = "APPROVED_FULL_SIZE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class TradeType(str, Enum):
    DAY_TRADE = "DAY_TRADE"
    SWING = "SWING"
    QUARTER = "QUARTER"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionType(str, Enum):
    SCANNER = "SCANNER"
    SIGNAL = "SIGNAL"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    JOURNAL = "JOURNAL"
    STRATEGY = "STRATEGY"
    AI_REVIEW = "AI_REVIEW"


class DecisionOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    CHANGED = "CHANGED"
    RECORDED = "RECORDED"


class ProviderReliabilityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class SignalStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    STALE_CANCELLED = "STALE_CANCELLED"


class SessionStatus(str, Enum):
    REGULAR = "regular"
    PREMARKET = "premarket"
    AFTER_HOURS = "after_hours"
    HOLIDAY = "holiday"
    EARLY_CLOSE = "early_close"
    CLOSED = "closed"


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    CHOPPY = "CHOPPY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    EARNINGS_SEASON = "EARNINGS_SEASON"
    MACRO_EVENT_RISK = "MACRO_EVENT_RISK"


class CatalystDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class RecommendationStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"

