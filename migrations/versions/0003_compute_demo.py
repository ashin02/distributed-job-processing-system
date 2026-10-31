"""Persist computational feed cursors and job membership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003_compute_demo"
down_revision = "0002_anonymous_demo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("request_key", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted", sa.Integer(), nullable=False),
        sa.Column("max_jobs", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("presets", JSONB(), nullable=False),
        sa.Column("reason", sa.String(100)),
        sa.UniqueConstraint("client_id", "request_key", name="uq_demo_runs_client_key"),
        sa.CheckConstraint("submitted >= 0 AND submitted <= max_jobs", name="ck_demo_run_count"),
        sa.CheckConstraint("max_jobs BETWEEN 1 AND 20", name="ck_demo_run_max_jobs"),
        sa.CheckConstraint("interval_seconds >= 1", name="ck_demo_run_interval"),
    )
    op.create_index("ix_demo_runs_client_started", "demo_runs", ["client_id", "started_at"])
    op.add_column("jobs", sa.Column("demo_run_id", UUID(as_uuid=True)))
    op.create_foreign_key("fk_jobs_demo_run", "jobs", "demo_runs", ["demo_run_id"], ["id"],
                          ondelete="RESTRICT")
    op.create_index("ix_jobs_demo_run_id", "jobs", ["demo_run_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_demo_run_id", table_name="jobs")
    op.drop_constraint("fk_jobs_demo_run", "jobs", type_="foreignkey")
    op.drop_column("jobs", "demo_run_id")
    op.drop_table("demo_runs")
