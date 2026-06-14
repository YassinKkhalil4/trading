"""migrate json columns to jsonb

Revision ID: 0011_migrate_json_columns_to_jsonb
Revises: 0009_clean_market_data_desc_index, 0010_drop_unused_intelligence_payloads
Create Date: 2026-06-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_migrate_json_columns_to_jsonb"
down_revision = ("0009_clean_market_data_desc_index", "0010_drop_unused_intelligence_payloads")
branch_labels = None
depends_on = None


_JSONB_COLUMNS = (
    ("provider_health_snapshots", "payload"),
    ("symbol_universe", "raw_asset_payload"),
    ("raw_market_data", "raw_payload"),
    ("raw_trade_ticks", "conditions"),
    ("raw_trade_ticks", "raw_payload"),
    ("raw_ingestion_events", "raw_payload"),
    ("market_data_stream_events", "payload"),
    ("raw_news", "raw_payload"),
    ("raw_filings", "raw_payload"),
    ("scheduler_runs", "payload"),
    ("worker_heartbeats", "payload"),
    ("data_quality_errors", "payload"),
    ("symbol_feature_snapshots", "snapshot"),
    ("sector_feature_snapshots", "snapshot"),
    ("strategy_registry", "allowed_timeframes"),
    ("strategy_registry", "allowed_regimes"),
    ("strategy_registry", "allowed_symbols"),
    ("strategy_approval_requests", "evidence"),
    ("scanner_results", "payload"),
    ("candidate_history", "payload"),
    ("signals", "entry_zone"),
    ("signal_versions", "payload"),
    ("signal_rejections", "payload"),
    ("risk_checks", "payload"),
    ("risk_rejections", "payload"),
    ("kill_switch_events", "payload"),
    ("exposure_snapshots", "sector_exposure"),
    ("exposure_snapshots", "strategy_exposure"),
    ("exposure_snapshots", "symbol_exposure"),
    ("broker_account_snapshots", "payload"),
    ("system_logs", "payload"),
    ("trade_journal", "rule_violations"),
    ("trade_journal", "mistake_tags"),
    ("audit_logs", "payload"),
    ("decision_logs", "payload"),
    ("weekly_reviews", "metrics"),
    ("backtest_reports", "assumptions"),
    ("backtest_reports", "metrics"),
    ("opportunity_scores", "component_scores"),
    ("opportunity_scores", "penalties"),
    ("opportunity_scores", "payload"),
    ("expectancy_snapshots", "payload"),
    ("strategy_performance_buckets", "payload"),
    ("sector_strength_snapshots", "payload"),
    ("symbol_relative_strength_snapshots", "payload"),
    ("alpha_rejection_reasons", "payload"),
)

_GIN_INDEXES = (
    ("ix_raw_market_data_raw_payload_gin", "raw_market_data", "raw_payload"),
    ("ix_raw_trade_ticks_raw_payload_gin", "raw_trade_ticks", "raw_payload"),
    ("ix_raw_ingestion_events_raw_payload_gin", "raw_ingestion_events", "raw_payload"),
    ("ix_raw_news_raw_payload_gin", "raw_news", "raw_payload"),
    ("ix_raw_filings_raw_payload_gin", "raw_filings", "raw_payload"),
    ("ix_system_logs_payload_gin", "system_logs", "payload"),
    ("ix_audit_logs_payload_gin", "audit_logs", "payload"),
    ("ix_decision_logs_payload_gin", "decision_logs", "payload"),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _alter_json_type(target_type: str) -> None:
    existing_tables = _tables()
    for table_name, column_name in _JSONB_COLUMNS:
        if table_name not in existing_tables or column_name not in _columns(table_name):
            continue
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" '
                f'ALTER COLUMN "{column_name}" TYPE {target_type} '
                f'USING "{column_name}"::{target_type}'
            )
        )


def upgrade() -> None:
    _alter_json_type("jsonb")

    existing_tables = _tables()
    for index_name, table_name, column_name in _GIN_INDEXES:
        if table_name not in existing_tables or column_name not in _columns(table_name):
            continue
        if index_name in _indexes(table_name):
            continue
        op.create_index(
            index_name,
            table_name,
            [column_name],
            unique=False,
            postgresql_using="gin",
        )


def downgrade() -> None:
    existing_tables = _tables()
    for index_name, table_name, _column_name in reversed(_GIN_INDEXES):
        if table_name in existing_tables and index_name in _indexes(table_name):
            op.drop_index(index_name, table_name=table_name)

    _alter_json_type("json")
