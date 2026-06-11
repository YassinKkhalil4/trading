from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_alpha_intelligence_layers"
down_revision = "0007_alpha_engine_tables"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _common_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    if not _has_table("point_in_time_universe_memberships"):
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
            *_common_columns(),
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
    if not _has_table("short_interest_snapshots"):
        op.create_table(
            "short_interest_snapshots",
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("short_interest_pct_float", sa.Float(), nullable=True),
            sa.Column("days_to_cover", sa.Float(), nullable=True),
            sa.Column("borrow_fee_pct", sa.Float(), nullable=True),
            sa.Column("utilization_pct", sa.Float(), nullable=True),
            sa.Column("float_shares", sa.Float(), nullable=True),
            sa.Column("short_score", sa.Float(), nullable=False),
            sa.Column("data_confidence", sa.Float(), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            *_common_columns(),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_table("options_intelligence_snapshots"):
        op.create_table(
            "options_intelligence_snapshots",
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("iv_rank", sa.Float(), nullable=True),
            sa.Column("iv_percentile", sa.Float(), nullable=True),
            sa.Column("open_interest", sa.Float(), nullable=True),
            sa.Column("gamma_exposure", sa.Float(), nullable=True),
            sa.Column("delta_exposure", sa.Float(), nullable=True),
            sa.Column("expected_move_pct", sa.Float(), nullable=True),
            sa.Column("options_score", sa.Float(), nullable=False),
            sa.Column("weekly_expiry", sa.Boolean(), nullable=False),
            sa.Column("earnings_expiry", sa.Boolean(), nullable=False),
            sa.Column("data_confidence", sa.Float(), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            *_common_columns(),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_table("multi_bagger_candidate_scores"):
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
            *_common_columns(),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    for table_name in (
        "multi_bagger_candidate_scores",
        "options_intelligence_snapshots",
        "short_interest_snapshots",
        "point_in_time_universe_memberships",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
