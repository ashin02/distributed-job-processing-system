"""Integration tests for the PostgreSQL persistence layer."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from computegrid_api.db.models import Job, JobAttempt, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, transaction
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


def make_job(*, idempotency_key: str = "request-1", state: JobState = JobState.ACCEPTED) -> Job:
    """Build a valid job for persistence tests."""
    return Job(
        id=uuid4(),
        client_id="test-client",
        idempotency_key=idempotency_key,
        request_hash="a" * 64,
        job_type="monte_carlo_pi",
        parameters={"samples": 1000, "seed": 42},
        state=state,
    )


def test_migration_creates_expected_schema(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    assert {"alembic_version", "jobs", "job_attempts", "outbox_events"} <= set(
        inspector.get_table_names()
    )
    assert "uq_jobs_client_idempotency_key" in {
        constraint["name"] for constraint in inspector.get_unique_constraints("jobs")
    }
    assert "ck_jobs_succeeded_has_result" in {
        constraint["name"] for constraint in inspector.get_check_constraints("jobs")
    }
    assert "ix_outbox_events_pending" in {
        index["name"] for index in inspector.get_indexes("outbox_events")
    }

    with migrated_engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "0001"


def test_transaction_commits_job_and_outbox_atomically(session_factory: SessionFactory) -> None:
    job = make_job()
    event = OutboxEvent(job=job, event_type="job.accepted", payload={"job_id": str(job.id)})

    with transaction(session_factory) as session:
        session.add_all([job, event])

    with session_factory() as session:
        persisted_job = session.scalar(select(Job).where(Job.id == job.id))
        persisted_event = session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job.id))

    assert persisted_job is not None
    assert persisted_job.created_at.tzinfo is not None
    assert persisted_event is not None
    assert persisted_event.publish_attempts == 0
    assert persisted_event.published_at is None


def test_transaction_rolls_back_all_writes(session_factory: SessionFactory) -> None:
    job = make_job()

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        transaction(session_factory) as session,
    ):
        session.add(job)
        raise RuntimeError("force rollback")

    with session_factory() as session:
        assert session.scalar(select(Job).where(Job.id == job.id)) is None


def test_client_idempotency_key_is_unique(session_factory: SessionFactory) -> None:
    with transaction(session_factory) as session:
        session.add(make_job())

    with pytest.raises(IntegrityError), transaction(session_factory) as session:
        session.add(make_job())


def test_attempt_constraints_and_foreign_key(session_factory: SessionFactory) -> None:
    job = make_job()
    with transaction(session_factory) as session:
        session.add(job)

    with pytest.raises(IntegrityError), transaction(session_factory) as session:
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=0,
                fencing_token=1,
            )
        )

    with pytest.raises(IntegrityError), transaction(session_factory) as session:
        session.add(
            JobAttempt(
                job_id=uuid4(),
                attempt_number=1,
                fencing_token=1,
            )
        )


def test_succeeded_job_requires_committed_result(session_factory: SessionFactory) -> None:
    invalid_job = make_job(state=JobState.SUCCEEDED)
    invalid_job.completed_at = datetime.now(UTC)

    with pytest.raises(IntegrityError), transaction(session_factory) as session:
        session.add(invalid_job)
