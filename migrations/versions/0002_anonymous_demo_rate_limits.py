"""Add shared anonymous-demo rate-limit counters.

Revision ID: 0002_anonymous_demo
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_anonymous_demo"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demo_rate_limits",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("request_count > 0", name="ck_demo_rate_limits_count_positive"),
        sa.PrimaryKeyConstraint("scope", "subject"),
    )
    op.create_index(
        "ix_demo_rate_limits_window_started_at",
        "demo_rate_limits",
        ["window_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_demo_rate_limits_window_started_at", table_name="demo_rate_limits")
    op.drop_table("demo_rate_limits")
