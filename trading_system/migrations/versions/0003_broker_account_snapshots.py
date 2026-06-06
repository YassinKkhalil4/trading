from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_broker_account_snapshots"
down_revision = "0002_trade_journal_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "broker_account_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "broker_account_snapshots",
        sa.Column("environment_mode", sa.String(length=32), nullable=False),
        sa.Column("broker", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("equity", sa.Float(), nullable=True),
        sa.Column("cash", sa.Float(), nullable=True),
        sa.Column("buying_power", sa.Float(), nullable=True),
        sa.Column("daytrade_count", sa.Integer(), nullable=True),
        sa.Column("pattern_day_trader", sa.Boolean(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broker_account_snapshots_account_id", "broker_account_snapshots", ["account_id"])
    op.create_index("ix_broker_account_snapshots_broker", "broker_account_snapshots", ["broker"])
    op.create_index(
        "ix_broker_account_snapshots_environment_mode",
        "broker_account_snapshots",
        ["environment_mode"],
    )
    op.create_index("ix_broker_account_snapshots_source_timestamp", "broker_account_snapshots", ["source_timestamp"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "broker_account_snapshots" in inspector.get_table_names():
        op.drop_table("broker_account_snapshots")
