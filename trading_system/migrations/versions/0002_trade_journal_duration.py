from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_trade_journal_duration"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("trade_journal")}
    if "time_in_trade_seconds" not in columns:
        op.add_column("trade_journal", sa.Column("time_in_trade_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("trade_journal")}
    if "time_in_trade_seconds" in columns:
        op.drop_column("trade_journal", "time_in_trade_seconds")
