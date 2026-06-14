"""add clean market data descending cursor index

Revision ID: 0009_clean_market_data_desc_index
Revises: cf5681ce63ec
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0009_clean_market_data_desc_index"
down_revision = "cf5681ce63ec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_clean_market_data_symbol_time_desc "
            "ON clean_market_data (symbol, source_timestamp DESC)"
        )
        return
    op.create_index(
        "ix_clean_market_data_symbol_time_desc",
        "clean_market_data",
        ["symbol", "source_timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_clean_market_data_symbol_time_desc", table_name="clean_market_data")
