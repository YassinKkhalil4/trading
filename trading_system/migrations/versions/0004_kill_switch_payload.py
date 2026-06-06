from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004_kill_switch_payload"
down_revision = "0003_broker_account_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("kill_switch_events")}
    if "payload" not in columns:
        op.add_column("kill_switch_events", sa.Column("payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("kill_switch_events")}
    if "payload" in columns:
        op.drop_column("kill_switch_events", "payload")
