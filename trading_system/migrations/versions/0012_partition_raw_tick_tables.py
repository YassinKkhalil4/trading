"""partition raw tick tables by source timestamp

Revision ID: 0012_partition_raw_tick_tables
Revises: 0011_migrate_json_columns_to_jsonb
Create Date: 2026-06-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_partition_raw_tick_tables"
down_revision = "0011_migrate_json_columns_to_jsonb"
branch_labels = None
depends_on = None


RAW_MARKET_DATA_TABLE = """
CREATE TABLE raw_market_data (
    provider VARCHAR(80) NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    source_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    raw_payload JSONB NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (provider, symbol, timeframe, source_timestamp)
) PARTITION BY RANGE (source_timestamp)
"""

RAW_TRADE_TICKS_TABLE = """
CREATE TABLE raw_trade_ticks (
    provider VARCHAR(80) NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    source_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    trade_id VARCHAR(128) NOT NULL,
    price FLOAT,
    size FLOAT,
    exchange VARCHAR(64),
    conditions JSONB,
    raw_payload JSONB NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (provider, symbol, source_timestamp, trade_id)
) PARTITION BY RANGE (source_timestamp)
"""

TABLE_DEFINITIONS = {
    "raw_market_data": {
        "old": "raw_market_data_old",
        "ddl": RAW_MARKET_DATA_TABLE,
        "default_partition": "raw_market_data_default",
        "columns": "provider, symbol, timeframe, source_timestamp, raw_payload, received_at, processed_at, created_at, updated_at",
        "index_names": (
            "raw_market_data_pkey",
            "ix_raw_market_data_symbol_time",
            "ix_raw_market_data_raw_payload_gin",
            "ix_raw_market_data_received_at",
            "ix_raw_market_data_processed_at",
        ),
        "indexes": (
            "CREATE INDEX ix_raw_market_data_symbol_time ON raw_market_data (symbol, source_timestamp)",
            "CREATE INDEX ix_raw_market_data_raw_payload_gin ON raw_market_data USING gin (raw_payload)",
            "CREATE INDEX ix_raw_market_data_received_at ON raw_market_data (received_at)",
            "CREATE INDEX ix_raw_market_data_processed_at ON raw_market_data (processed_at)",
        ),
    },
    "raw_trade_ticks": {
        "old": "raw_trade_ticks_old",
        "ddl": RAW_TRADE_TICKS_TABLE,
        "default_partition": "raw_trade_ticks_default",
        "columns": "provider, symbol, source_timestamp, trade_id, price, size, exchange, conditions, raw_payload, received_at, processed_at, created_at, updated_at",
        "index_names": (
            "raw_trade_ticks_pkey",
            "ix_raw_trade_ticks_symbol_time",
            "ix_raw_trade_ticks_raw_payload_gin",
            "ix_raw_trade_ticks_received_at",
            "ix_raw_trade_ticks_processed_at",
        ),
        "indexes": (
            "CREATE INDEX ix_raw_trade_ticks_symbol_time ON raw_trade_ticks (symbol, source_timestamp)",
            "CREATE INDEX ix_raw_trade_ticks_raw_payload_gin ON raw_trade_ticks USING gin (raw_payload)",
            "CREATE INDEX ix_raw_trade_ticks_received_at ON raw_trade_ticks (received_at)",
            "CREATE INDEX ix_raw_trade_ticks_processed_at ON raw_trade_ticks (processed_at)",
        ),
    },
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _rename_existing_indexes(definition: dict[str, object]) -> None:
    for index_name in definition["index_names"]:  # type: ignore[index]
        op.execute(sa.text(f"ALTER INDEX IF EXISTS {index_name} RENAME TO {index_name}_old"))


def _create_indexes(definition: dict[str, object]) -> None:
    for index_sql in definition["indexes"]:  # type: ignore[index]
        op.execute(sa.text(str(index_sql)))


def _recreate_as_partitioned(table_name: str, definition: dict[str, object]) -> None:
    existing_tables = _tables()
    old_table = str(definition["old"])
    default_partition = str(definition["default_partition"])
    columns = str(definition["columns"])

    if table_name not in existing_tables:
        op.execute(sa.text(str(definition["ddl"])))
        op.execute(sa.text(f"CREATE TABLE {default_partition} PARTITION OF {table_name} DEFAULT"))
        _create_indexes(definition)
        return

    op.rename_table(table_name, old_table)
    _rename_existing_indexes(definition)
    op.execute(sa.text(str(definition["ddl"])))
    op.execute(sa.text(f"CREATE TABLE {default_partition} PARTITION OF {table_name} DEFAULT"))
    _create_indexes(definition)
    op.execute(sa.text(f"INSERT INTO {table_name} ({columns}) SELECT {columns} FROM {old_table}"))
    op.drop_table(old_table)


def upgrade() -> None:
    for table_name, definition in TABLE_DEFINITIONS.items():
        _recreate_as_partitioned(table_name, definition)


def downgrade() -> None:
    for table_name, definition in reversed(TABLE_DEFINITIONS.items()):
        old_table = str(definition["old"])
        columns = str(definition["columns"])
        op.rename_table(table_name, old_table)
        _rename_existing_indexes(definition)
        op.execute(sa.text(str(definition["ddl"]).replace(" PARTITION BY RANGE (source_timestamp)", "")))
        _create_indexes(definition)
        op.execute(sa.text(f"INSERT INTO {table_name} ({columns}) SELECT {columns} FROM {old_table}"))
        op.drop_table(old_table)
