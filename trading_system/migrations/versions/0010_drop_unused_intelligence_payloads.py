"""drop unused intelligence payload columns

Revision ID: 0010_drop_unused_intelligence_payloads
Revises: cf5681ce63ec
Create Date: 2026-06-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_drop_unused_intelligence_payloads"
down_revision = "cf5681ce63ec"
branch_labels = None
depends_on = None


_TABLES = ("short_interest_snapshots", "options_intelligence_snapshots")


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    for table_name in _TABLES:
        if _has_column(table_name, "payload"):
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_column("payload")


def downgrade() -> None:
    for table_name in _TABLES:
        if not _has_column(table_name, "payload"):
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.add_column(sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))
            op.alter_column(table_name, "payload", server_default=None)
