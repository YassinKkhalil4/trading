from __future__ import annotations

from trading_system.app.catalysts.news_classifier import classify_news_headline
from trading_system.app.monitoring.trade_monitor import evaluate_day_trade_to_swing_conversion


def test_news_classifier_flags_duplicate_and_rumor():
    first = classify_news_headline(headline="Report: AMD reportedly wins new customer", source="blog")
    second = classify_news_headline(
        headline="Report: AMD reportedly wins new customer",
        source="blog",
        seen_hashes={first.normalized_headline_hash},
    )
    assert second.duplicate_headline is True
    assert second.rumor_flag is True
    assert second.source_confidence_score <= 35


def test_never_convert_losing_day_trade_to_swing():
    decision = evaluate_day_trade_to_swing_conversion(
        profitable=False,
        close_near_high_of_day=True,
        volume_confirms=True,
        catalyst_still_valid=True,
        overnight_risk_approved=True,
        market_regime_supportive=True,
    )
    assert decision.action == "block_conversion"
    assert "Never convert" in decision.reason

