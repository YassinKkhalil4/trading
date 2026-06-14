"""partition raw tick tables by source timestamp

Revision ID: 0012_partition_raw_tick_tables
Revises: 0011_migrate_json_columns_to_jsonb
Create Date: 2026-06-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op


revision = "0012_partition_raw_tick_tables"
down_revision = "0011_migrate_json_columns_to_jsonb"
branch_labels = None
depends_on = None


PARTITIONED_TABLES = ("raw_market_data", "raw_trade_ticks")


def _partition_table(table_name: str) -> None:
    old_table_name = f"{table_name}_old"
    default_partition_name = f"{table_name}_default"

    op.execute(f"ALTER TABLE {table_name} RENAME TO {old_table_name}")
    op.execute(
        f"CREATE TABLE {table_name} "
        f"(LIKE {old_table_name} INCLUDING ALL) "
        "PARTITION BY RANGE (source_timestamp)"
    )
    op.execute(f"CREATE TABLE {default_partition_name} PARTITION OF {table_name} DEFAULT")
    op.execute(f"INSERT INTO {table_name} SELECT * FROM {old_table_name}")
    op.execute(f"DROP TABLE {old_table_name}")


def upgrade() -> None:
    for table_name in PARTITIONED_TABLES:
        _partition_table(table_name)


def _unpartition_table(table_name: str) -> None:
    old_table_name = f"{table_name}_old"

    op.execute(f"ALTER TABLE {table_name} RENAME TO {old_table_name}")
    op.execute(f"CREATE TABLE {table_name} (LIKE {old_table_name} INCLUDING ALL)")
    op.execute(f"INSERT INTO {table_name} SELECT * FROM {old_table_name}")
    op.execute(f"DROP TABLE {old_table_name}")


def downgrade() -> None:
    for table_name in reversed(PARTITIONED_TABLES):
        _unpartition_table(table_name)
