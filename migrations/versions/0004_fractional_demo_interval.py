"""Allow fractional computational-feed submission intervals.

Revision ID: 0004_fractional_demo_interval
Revises: 0003_compute_demo
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_fractional_demo_interval"
down_revision: str | None = "0003_compute_demo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "demo_runs",
        "interval_seconds",
        existing_type=sa.Integer(),
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="interval_seconds::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "demo_runs",
        "interval_seconds",
        existing_type=sa.Float(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="round(interval_seconds)::integer",
    )
