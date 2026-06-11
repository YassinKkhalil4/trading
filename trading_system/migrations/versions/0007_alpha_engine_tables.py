from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_alpha_engine_tables"
down_revision = "0006_clean_news_sentiment"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("opportunity_scores"):
        op.create_table(
            "opportunity_scores",
            sa.Column("scanner_result_id", sa.String(length=36), nullable=True),
            sa.Column("signal_id", sa.String(length=36), nullable=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("strategy_id", sa.String(length=80), nullable=False),
            sa.Column("setup_type", sa.String(length=80), nullable=True),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("grade", sa.String(length=16), nullable=False),
            sa.Column("component_scores", sa.JSON(), nullable=False),
            sa.Column("penalties", sa.JSON(), nullable=False),
            sa.Column("explanation", sa.Text(), nullable=False),
            sa.Column("expected_r", sa.Float(), nullable=True),
            sa.Column("historical_win_rate", sa.Float(), nullable=True),
            sa.Column("expectancy_sample_size", sa.Integer(), nullable=False),
            sa.Column("confidence_level", sa.Float(), nullable=False),
            sa.Column("suggested_risk_multiplier", sa.Float(), nullable=False),
            sa.Column("market_regime", sa.String(length=64), nullable=True),
            sa.Column("sector_regime", sa.String(length=64), nullable=True),
            sa.Column("catalyst_type", sa.String(length=80), nullable=True),
            sa.Column("linked_news_id", sa.String(length=36), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["scanner_result_id"], ["scanner_results.id"]),
            sa.ForeignKeyConstraint(["signal_id"], ["signals.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_table("opportunity_score_components"):
        op.create_table(
            "opportunity_score_components",
            sa.Column("opportunity_score_id", sa.String(length=36), nullable=False),
            sa.Column("component_name", sa.String(length=80), nullable=False),
            sa.Column("raw_value", sa.Float(), nullable=True),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False),
            sa.Column("explanation", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["opportunity_score_id"], ["opportunity_scores.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    tables = {
        "expectancy_snapshots": [
            sa.Column("bucket_type", sa.String(length=80), nullable=False),
            sa.Column("bucket_key", sa.String(length=160), nullable=False),
            sa.Column("strategy_id", sa.String(length=80), nullable=True),
            sa.Column("setup_type", sa.String(length=80), nullable=True),
            sa.Column("sample_size", sa.Integer(), nullable=False),
            sa.Column("win_rate", sa.Float(), nullable=True),
            sa.Column("average_win", sa.Float(), nullable=True),
            sa.Column("average_loss", sa.Float(), nullable=True),
            sa.Column("expectancy_r", sa.Float(), nullable=True),
            sa.Column("profit_factor", sa.Float(), nullable=True),
            sa.Column("max_drawdown", sa.Float(), nullable=True),
            sa.Column("average_hold_seconds", sa.Float(), nullable=True),
            sa.Column("average_slippage_bps", sa.Float(), nullable=True),
            sa.Column("average_mfe", sa.Float(), nullable=True),
            sa.Column("average_mae", sa.Float(), nullable=True),
            sa.Column("confidence_level", sa.Float(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        ],
        "strategy_performance_buckets": [
            sa.Column("strategy_id", sa.String(length=80), nullable=False),
            sa.Column("setup_type", sa.String(length=80), nullable=True),
            sa.Column("bucket_type", sa.String(length=80), nullable=False),
            sa.Column("bucket_key", sa.String(length=160), nullable=False),
            sa.Column("sample_size", sa.Integer(), nullable=False),
            sa.Column("expectancy_r", sa.Float(), nullable=True),
            sa.Column("win_rate", sa.Float(), nullable=True),
            sa.Column("recent_expectancy_r", sa.Float(), nullable=True),
            sa.Column("decay_warning", sa.Boolean(), nullable=False),
            sa.Column("confidence_level", sa.Float(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        ],
        "sector_strength_snapshots": [
            sa.Column("sector", sa.String(length=128), nullable=False),
            sa.Column("sector_etf", sa.String(length=16), nullable=True),
            sa.Column("sector_score", sa.Float(), nullable=False),
            sa.Column("sector_vs_spy_score", sa.Float(), nullable=True),
            sa.Column("breadth_score", sa.Float(), nullable=True),
            sa.Column("regime", sa.String(length=64), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        ],
        "symbol_relative_strength_snapshots": [
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("sector", sa.String(length=128), nullable=True),
            sa.Column("sector_etf", sa.String(length=16), nullable=True),
            sa.Column("stock_vs_spy_score", sa.Float(), nullable=True),
            sa.Column("stock_vs_sector_score", sa.Float(), nullable=True),
            sa.Column("leadership_rank", sa.Integer(), nullable=True),
            sa.Column("candidate_reason", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        ],
        "alpha_rejection_reasons": [
            sa.Column("scanner_result_id", sa.String(length=36), nullable=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("strategy_id", sa.String(length=80), nullable=True),
            sa.Column("setup_type", sa.String(length=80), nullable=True),
            sa.Column("reason_code", sa.String(length=80), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("severity", sa.String(length=32), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
        ],
        "strategy_setup_tags": [
            sa.Column("strategy_id", sa.String(length=80), nullable=False),
            sa.Column("setup_type", sa.String(length=80), nullable=False),
            sa.Column("tag", sa.String(length=80), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.UniqueConstraint("strategy_id", "setup_type", "tag", name="uq_strategy_setup_tag"),
        ],
    }
    for table_name, columns in tables.items():
        if _has_table(table_name):
            continue
        op.create_table(
            table_name,
            *columns,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    for table_name in (
        "strategy_setup_tags",
        "alpha_rejection_reasons",
        "symbol_relative_strength_snapshots",
        "sector_strength_snapshots",
        "strategy_performance_buckets",
        "expectancy_snapshots",
        "opportunity_score_components",
        "opportunity_scores",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
