from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_decision_support_scorecards"
down_revision = "0005_master_universe_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "decision_support_artifacts" not in tables:
        op.create_table(
            "decision_support_artifacts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("artifact_type", sa.String(length=80), nullable=False),
            sa.Column("provider_name", sa.String(length=80), nullable=False),
            sa.Column("provider_version", sa.String(length=80), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("input_payload_hash", sa.String(length=64), nullable=False),
            sa.Column("input_payload", sa.JSON(), nullable=False),
            sa.Column("output_payload", sa.JSON(), nullable=False),
            sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="ACCEPTED"),
            sa.Column("validation_reason", sa.Text(), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("reason", sa.Text(), nullable=False),
        )
        _indexes(
            "decision_support_artifacts",
            ("artifact_type", "provider_name", "provider_version", "prompt_version", "input_payload_hash"),
            ("validation_status", "fallback_used", "source_timestamp"),
        )

    if "opportunity_scorecard_snapshots" not in tables:
        op.create_table(
            "opportunity_scorecard_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scanner_result_id", sa.String(length=36), nullable=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("strategy_id", sa.String(length=80), nullable=False),
            sa.Column("scanner_name", sa.String(length=80), nullable=False),
            sa.Column("scorecard_version", sa.String(length=80), nullable=False),
            sa.Column("opportunity_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("grade", sa.String(length=32), nullable=False),
            sa.Column("component_scores", sa.JSON(), nullable=False),
            sa.Column("reasons", sa.JSON(), nullable=False),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("missing_data", sa.JSON(), nullable=False),
            sa.Column("grade_rationale", sa.Text(), nullable=False),
        )
        _indexes(
            "opportunity_scorecard_snapshots",
            ("scanner_result_id", "symbol", "strategy_id", "scanner_name", "scorecard_version", "grade"),
            ("source_timestamp",),
        )

    if "scorecard_evaluations" not in tables:
        op.create_table(
            "scorecard_evaluations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scorecard_version", sa.String(length=80), nullable=False),
            sa.Column("grade", sa.String(length=32), nullable=False),
            sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("win_rate", sa.Float(), nullable=True),
            sa.Column("average_pnl", sa.Float(), nullable=True),
            sa.Column("average_r", sa.Float(), nullable=True),
            sa.Column("average_slippage_bps", sa.Float(), nullable=True),
            sa.Column("rule_violation_rate", sa.Float(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
        )
        _indexes("scorecard_evaluations", ("scorecard_version", "grade", "source_timestamp"))

    _add_column("news_catalyst_scores", "taxonomy", sa.Column("taxonomy", sa.JSON(), nullable=True))
    _add_column(
        "trade_theses",
        "decision_support_artifact_id",
        sa.Column("decision_support_artifact_id", sa.String(length=36), nullable=True),
    )
    _add_column("trade_theses", "support_payload", sa.Column("support_payload", sa.JSON(), nullable=True))
    _add_column(
        "ai_reviews",
        "decision_support_artifact_id",
        sa.Column("decision_support_artifact_id", sa.String(length=36), nullable=True),
    )
    _add_column("ai_reviews", "structured_payload", sa.Column("structured_payload", sa.JSON(), nullable=True))
    _add_column(
        "strategy_recommendations",
        "severity",
        sa.Column("severity", sa.String(length=32), nullable=False, server_default="LOW"),
    )
    _add_column("strategy_recommendations", "evidence", sa.Column("evidence", sa.JSON(), nullable=True))
    _add_column(
        "strategy_recommendations",
        "decision_support_artifact_id",
        sa.Column("decision_support_artifact_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table, columns in (
        ("strategy_recommendations", ("decision_support_artifact_id", "evidence", "severity")),
        ("ai_reviews", ("structured_payload", "decision_support_artifact_id")),
        ("trade_theses", ("support_payload", "decision_support_artifact_id")),
        ("news_catalyst_scores", ("taxonomy",)),
    ):
        if table in tables:
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column in columns:
                if column in existing:
                    op.drop_column(table, column)

    for table in (
        "scorecard_evaluations",
        "opportunity_scorecard_snapshots",
        "decision_support_artifacts",
    ):
        if table in tables:
            op.drop_table(table)


def _add_column(table_name: str, column_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return
    columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column_name not in columns:
        op.add_column(table_name, column)


def _indexes(table_name: str, columns: tuple[str, ...], extra: tuple[str, ...] = ()) -> None:
    for column in columns + extra:
        op.create_index(f"ix_{table_name}_{column}", table_name, [column])
