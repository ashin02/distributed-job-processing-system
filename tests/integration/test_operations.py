"""PostgreSQL integration coverage for operational job counts."""

from uuid import uuid4

import pytest
from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState
from computegrid_api.db.session import SessionFactory
from computegrid_api.operations import DependencyStatus, postgresql_summary, visitor_jobs_summary

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


def test_visitor_jobs_summary_is_scoped_and_correlates_persisted_worker(
    session_factory: SessionFactory,
) -> None:
    owner_job = Job(
        id=uuid4(),
        client_id="visitor-a",
        idempotency_key="owner-running",
        request_hash="a" * 64,
        job_type="monte_carlo_pi",
        parameters={"samples": 100, "seed": 1},
        state=JobState.RUNNING,
    )
    other_job = Job(
        id=uuid4(),
        client_id="visitor-b",
        idempotency_key="other-running",
        request_hash="b" * 64,
        job_type="bqm",
        parameters={"vartype": "BINARY"},
        state=JobState.RUNNING,
    )
    with session_factory.begin() as session:
        session.add_all(
            [
                owner_job,
                other_job,
                JobAttempt(
                    job=owner_job,
                    attempt_number=1,
                    fencing_token=1,
                    state=AttemptState.RUNNING,
                    worker_id="celery@worker-a",
                ),
                JobAttempt(
                    job=other_job,
                    attempt_number=1,
                    fencing_token=1,
                    state=AttemptState.RUNNING,
                    worker_id="celery@worker-b",
                ),
            ]
        )

    status, jobs = visitor_jobs_summary(session_factory, client_id="visitor-a")

    assert status == DependencyStatus.AVAILABLE
    assert [job.job_id for job in jobs] == [owner_job.id]
    assert jobs[0].worker_key == "celery@worker-a"
