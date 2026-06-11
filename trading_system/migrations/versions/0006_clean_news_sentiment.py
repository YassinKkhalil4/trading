from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_clean_news_sentiment"
down_revision = "0005_master_universe_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("clean_news")}

    if "sentiment_score" not in columns:
        op.add_column("clean_news", sa.Column("sentiment_score", sa.Float(), nullable=True))
    if "relevance_score" not in columns:
        op.add_column("clean_news", sa.Column("relevance_score", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("clean_news")}

    for column in ("relevance_score", "sentiment_score"):
        if column in columns:
            op.drop_column("clean_news", column)
