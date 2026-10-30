"""PostgreSQL integration coverage for operational job counts."""

from uuid import uuid4

import pytest
from computegrid_api.db.models import Job, JobState
from computegrid_api.db.session import SessionFactory
from computegrid_api.operations import DependencyStatus, postgresql_summary

pytestmark = pytest.mark.integration


def test_postgresql_summary_groups_jobs_by_state(session_factory: SessionFactory) -> None:
    with session_factory.begin() as session:
        for index, state in enumerate(
            [JobState.ACCEPTED, JobState.QUEUED, JobState.QUEUED, JobState.FAILED]
        ):
            session.add(
                Job(
                    id=uuid4(),
                    client_id="operations-test",
                    idempotency_key=f"operations-{index}",
                    request_hash=f"{index:064x}",
                    job_type="monte_carlo_pi",
                    parameters={"samples": 100, "seed": index},
                    state=state,
                )
            )

    summary = postgresql_summary(session_factory, timeout_seconds=2)

    assert summary.status == DependencyStatus.AVAILABLE
    assert summary.job_counts is not None
    assert summary.job_counts[JobState.ACCEPTED] == 1
    assert summary.job_counts[JobState.QUEUED] == 2
    assert summary.job_counts[JobState.FAILED] == 1
    assert summary.job_counts[JobState.RUNNING] == 0
