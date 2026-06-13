"""purge unused ai models and flatten system logs

Revision ID: cf5681ce63ec
Revises: 0008_alpha_intelligence_layers
Create Date: 2026-06-13 23:21:15.629681
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "cf5681ce63ec"
down_revision = "0008_alpha_intelligence_layers"
branch_labels = None
depends_on = None


_SYSTEM_LOG_CHILD_COLUMNS = (
    "environment_mode",
    "broker",
    "mismatch_detected",
    "order_id",
    "error_type",
    "check_name",
    "passed",
    "overall_status",
    "live_allowed",
    "checks",
    "approved_by",
    "expires_at",
    "revoked_at",
    "revoke_reason",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name in indexes:
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    existing_tables = _tables()

    if "point_in_time_universe_memberships" in existing_tables:
        _drop_index_if_exists(
            "ix_pit_universe_asof_symbol", "point_in_time_universe_memberships"
        )

    if "ai_reviews" in existing_tables:
        op.drop_table("ai_reviews")
    if "ai_prompt_templates" in existing_tables:
        op.drop_table("ai_prompt_templates")
    if "trade_theses" in existing_tables:
        op.drop_table("trade_theses")
    if "multi_bagger_candidate_scores" in existing_tables:
        op.drop_table("multi_bagger_candidate_scores")
    if "point_in_time_universe_memberships" in existing_tables:
        op.drop_table("point_in_time_universe_memberships")

    if "system_logs" in existing_tables:
        existing_columns = _columns("system_logs")
        columns_to_drop = [
            column_name
            for column_name in _SYSTEM_LOG_CHILD_COLUMNS
            if column_name in existing_columns
        ]
        if columns_to_drop:
            with op.batch_alter_table("system_logs") as batch_op:
                for column_name in columns_to_drop:
                    batch_op.drop_column(column_name)

        if op.get_bind().dialect.name == "postgresql":
            op.alter_column(
                "system_logs",
                "payload",
                existing_type=sa.JSON(),
                type_=postgresql.JSONB(astext_type=sa.Text()),
                postgresql_using="payload::jsonb",
                existing_nullable=True,
            )


def downgrade() -> None:
    existing_tables = _tables()

    if "trade_theses" not in existing_tables:
        op.create_table(
            "trade_theses",
            sa.Column("signal_id", sa.String(length=36), nullable=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("strategy_id", sa.String(length=80), nullable=False),
            sa.Column("prompt_version", sa.String(length=32), nullable=False),
            sa.Column("trade_type", sa.String(length=40), nullable=False),
            sa.Column("setup_quality", sa.Float(), nullable=False),
            sa.Column("catalyst_quality", sa.Float(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("reason_for_trade", sa.Text(), nullable=False),
            sa.Column("invalidation_reason", sa.Text(), nullable=False),
            sa.Column("risks", sa.JSON(), nullable=False),
            sa.Column("suggested_holding_period", sa.String(length=80), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["signal_id"], ["signals.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_trade_theses_signal_id", "trade_theses", ["signal_id"])
        op.create_index("ix_trade_theses_source_timestamp", "trade_theses", ["source_timestamp"])
        op.create_index("ix_trade_theses_strategy_id", "trade_theses", ["strategy_id"])
        op.create_index("ix_trade_theses_symbol", "trade_theses", ["symbol"])

    if "ai_prompt_templates" not in existing_tables:
        op.create_table(
            "ai_prompt_templates",
            sa.Column("template_name", sa.String(length=80), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("prompt_text", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("change_reason", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("template_name", "version", name="uq_prompt_template_version"),
        )
        op.create_index("ix_ai_prompt_templates_source_timestamp", "ai_prompt_templates", ["source_timestamp"])
        op.create_index("ix_ai_prompt_templates_template_name", "ai_prompt_templates", ["template_name"])
        op.create_index("ix_ai_prompt_templates_version", "ai_prompt_templates", ["version"])

    if "ai_reviews" not in existing_tables:
        op.create_table(
            "ai_reviews",
            sa.Column("trade_journal_id", sa.String(length=36), nullable=True),
            sa.Column("prompt_template_id", sa.String(length=36), nullable=True),
            sa.Column("prompt_version", sa.String(length=32), nullable=False),
            sa.Column("review_text", sa.Text(), nullable=False),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["prompt_template_id"], ["ai_prompt_templates.id"]),
            sa.ForeignKeyConstraint(["trade_journal_id"], ["trade_journal.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_reviews_source_timestamp", "ai_reviews", ["source_timestamp"])
        op.create_index("ix_ai_reviews_trade_journal_id", "ai_reviews", ["trade_journal_id"])

    if "point_in_time_universe_memberships" not in existing_tables:
        op.create_table(
            "point_in_time_universe_memberships",
            sa.Column("universe_name", sa.String(length=80), nullable=False),
            sa.Column("as_of_date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("asset_class", sa.String(length=32), nullable=False),
            sa.Column("exchange", sa.String(length=32), nullable=True),
            sa.Column("sector", sa.String(length=128), nullable=True),
            sa.Column("industry", sa.String(length=128), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("is_tradable", sa.Boolean(), nullable=False),
            sa.Column("is_liquid", sa.Boolean(), nullable=False),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delisted", sa.Boolean(), nullable=False),
            sa.Column("membership_reason", sa.Text(), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "universe_name", "as_of_date", "symbol", name="uq_pit_universe_symbol_date"
            ),
        )
        op.create_index(
            "ix_pit_universe_asof_symbol",
            "point_in_time_universe_memberships",
            ["as_of_date", "symbol"],
        )

    if "multi_bagger_candidate_scores" not in existing_tables:
        op.create_table(
            "multi_bagger_candidate_scores",
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("horizon", sa.String(length=32), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("grade", sa.String(length=16), nullable=False),
            sa.Column("target_multiple", sa.String(length=16), nullable=False),
            sa.Column("component_scores", sa.JSON(), nullable=False),
            sa.Column("narrative", sa.Text(), nullable=False),
            sa.Column("growth_score", sa.Float(), nullable=True),
            sa.Column("capital_flows_score", sa.Float(), nullable=True),
            sa.Column("institutional_accumulation_score", sa.Float(), nullable=True),
            sa.Column("short_squeeze_score", sa.Float(), nullable=True),
            sa.Column("options_leverage_score", sa.Float(), nullable=True),
            sa.Column("risk_flags", sa.JSON(), nullable=False),
            sa.Column("confidence_level", sa.Float(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
