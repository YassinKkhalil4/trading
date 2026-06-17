from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0013_market_regime_hmm_probabilities"
down_revision = "0012_partition_raw_tick_tables"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("market_regime_snapshots", "hmm_state_probabilities"):
        op.add_column("market_regime_snapshots", sa.Column("hmm_state_probabilities", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("market_regime_snapshots", "hmm_state_probabilities"):
        op.drop_column("market_regime_snapshots", "hmm_state_probabilities")
