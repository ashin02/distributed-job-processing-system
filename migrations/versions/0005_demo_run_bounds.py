"""Align computational-feed constraints with the M5C settings.

Revision ID: 0005_demo_run_bounds
Revises: 0004_fractional_demo_interval
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_demo_run_bounds"
down_revision: str | None = "0004_fractional_demo_interval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE demo_runs DROP CONSTRAINT IF EXISTS ck_demo_run_interval"
    )
    op.execute(
        "ALTER TABLE demo_runs DROP CONSTRAINT IF EXISTS ck_demo_run_max_jobs"
    )
    op.create_check_constraint(
        "ck_demo_run_interval",
        "demo_runs",
        "interval_seconds >= 0.1 AND interval_seconds <= 20",
    )
    op.create_check_constraint(
        "ck_demo_run_max_jobs",
        "demo_runs",
        "max_jobs >= 1 AND max_jobs <= 40",
    )


def downgrade() -> None:
    op.drop_constraint("ck_demo_run_max_jobs", "demo_runs", type_="check")
    op.drop_constraint("ck_demo_run_interval", "demo_runs", type_="check")
    op.create_check_constraint(
        "ck_demo_run_interval",
        "demo_runs",
        "interval_seconds >= 1",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_demo_run_max_jobs",
        "demo_runs",
        "max_jobs >= 1 AND max_jobs <= 20",
        postgresql_not_valid=True,
    )
