from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_master_universe_fields"
down_revision = "0004_kill_switch_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("symbol_universe")}

    if "is_liquid" not in columns:
        op.add_column(
            "symbol_universe",
            sa.Column("is_liquid", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "disable_reason" not in columns:
        op.add_column("symbol_universe", sa.Column("disable_reason", sa.String(length=64), nullable=True))
    if "provider_asset_id" not in columns:
        op.add_column("symbol_universe", sa.Column("provider_asset_id", sa.String(length=64), nullable=True))
    if "provider_status" not in columns:
        op.add_column("symbol_universe", sa.Column("provider_status", sa.String(length=32), nullable=True))
    if "last_provider_check_at" not in columns:
        op.add_column("symbol_universe", sa.Column("last_provider_check_at", sa.DateTime(timezone=True), nullable=True))
    if "latest_price" not in columns:
        op.add_column("symbol_universe", sa.Column("latest_price", sa.Float(), nullable=True))
    if "average_volume" not in columns:
        op.add_column("symbol_universe", sa.Column("average_volume", sa.Float(), nullable=True))
    if "dollar_volume" not in columns:
        op.add_column("symbol_universe", sa.Column("dollar_volume", sa.Float(), nullable=True))
    if "spread_bps" not in columns:
        op.add_column("symbol_universe", sa.Column("spread_bps", sa.Float(), nullable=True))
    if "liquidity_rank" not in columns:
        op.add_column("symbol_universe", sa.Column("liquidity_rank", sa.Integer(), nullable=True))
    if "raw_asset_payload" not in columns:
        op.add_column("symbol_universe", sa.Column("raw_asset_payload", sa.JSON(), nullable=True))

    indexes = {index["name"] for index in inspector.get_indexes("symbol_universe")}
    for index_name, column in (
        ("ix_symbol_universe_is_liquid", "is_liquid"),
        ("ix_symbol_universe_disable_reason", "disable_reason"),
        ("ix_symbol_universe_provider_asset_id", "provider_asset_id"),
        ("ix_symbol_universe_provider_status", "provider_status"),
        ("ix_symbol_universe_liquidity_rank", "liquidity_rank"),
    ):
        if index_name not in indexes:
            op.create_index(index_name, "symbol_universe", [column])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("symbol_universe")}
    for index_name in (
        "ix_symbol_universe_liquidity_rank",
        "ix_symbol_universe_provider_status",
        "ix_symbol_universe_provider_asset_id",
        "ix_symbol_universe_disable_reason",
        "ix_symbol_universe_is_liquid",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name="symbol_universe")

    columns = {column["name"] for column in inspector.get_columns("symbol_universe")}
    for column in (
        "raw_asset_payload",
        "liquidity_rank",
        "spread_bps",
        "dollar_volume",
        "average_volume",
        "latest_price",
        "last_provider_check_at",
        "provider_status",
        "provider_asset_id",
        "disable_reason",
        "is_liquid",
    ):
        if column in columns:
            op.drop_column("symbol_universe", column)
