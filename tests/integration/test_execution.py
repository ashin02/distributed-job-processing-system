"""PostgreSQL integration tests for relay and execution state transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.models import AttemptState, Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory
from computegrid_api.jobs.execution import ExecutionDisposition, execute_job
from computegrid_api.jobs.repository import JobRepository
from computegrid_api.outbox_relay import relay_once
from sqlalchemy import select

pytestmark = pytest.mark.integration


def create_job(session_factory: SessionFactory, key: str = "execution-1") -> UUID:
    repository = JobRepository(session_factory)
    created = repository.create(
        client_id="execution-client",
        idempotency_key=key,
        request=JobCreateRequest.model_validate(
            {
                "type": "monte_carlo_pi",
                "parameters": {"samples": 10_000, "seed": 42},
            }
        ),
    )
    return created.job.id


@dataclass
class RecordingPublisher:
    published: list[UUID] = field(default_factory=list)
    error: Exception | None = None

    def publish(self, event: OutboxEvent) -> None:
        if self.error is not None:
            raise self.error
        self.published.append(event.id)


def test_relay_marks_confirmed_event_and_job_queued(session_factory: SessionFactory) -> None:
    job_id = create_job(session_factory)
    publisher = RecordingPublisher()

    assert relay_once(session_factory, publisher, batch_size=10) == 1

    with session_factory() as session:
        job = session.get(Job, job_id)
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
        assert job is not None
        assert event is not None
        assert job.state == JobState.QUEUED
        assert job.queued_at is not None
        assert event.published_at is not None
        assert event.publish_attempts == 1
        assert event.last_error is None
    assert publisher.published == [event.id]


def test_relay_keeps_unconfirmed_event_pending(session_factory: SessionFactory) -> None:
    job_id = create_job(session_factory)
    publisher = RecordingPublisher(error=RuntimeError("broker unavailable"))

    assert relay_once(session_factory, publisher, batch_size=10) == 0

    with session_factory() as session:
        job = session.get(Job, job_id)
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
        assert job is not None
        assert event is not None
        assert job.state == JobState.ACCEPTED
        assert event.published_at is None
        assert event.publish_attempts == 1
        assert event.next_attempt_at is not None
        assert event.last_error == "broker unavailable"


def test_execution_persists_attempt_and_result_idempotently(
    session_factory: SessionFactory,
) -> None:
    job_id = create_job(session_factory)

    assert (
        execute_job(job_id, worker_id="worker-1", session_factory=session_factory)
        == ExecutionDisposition.COMPLETED
    )
    assert (
        execute_job(job_id, worker_id="worker-duplicate", session_factory=session_factory)
        == ExecutionDisposition.ALREADY_TERMINAL
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.SUCCEEDED
        assert job.result == {"pi_estimate": 3.126, "samples": 10_000, "seed": 42}
        assert job.started_at is not None
        assert job.completed_at is not None
        assert len(job.attempts) == 1
        assert job.attempts[0].state == AttemptState.SUCCEEDED
        assert job.attempts[0].worker_id == "worker-1"
        assert job.attempts[0].finished_at is not None


def test_execution_persists_terminal_failure(session_factory: SessionFactory) -> None:
    job_id = create_job(session_factory)

    def fail(_: dict[str, object]) -> dict[str, object]:
        raise ValueError("invalid computation")

    assert (
        execute_job(
            job_id,
            worker_id="worker-1",
            session_factory=session_factory,
            computation=fail,
        )
        == ExecutionDisposition.FAILED
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.FAILED
        assert job.result is None
        assert job.error == {"type": "ValueError", "message": "invalid computation"}
        assert len(job.attempts) == 1
        assert job.attempts[0].state == AttemptState.FAILED
        assert job.attempts[0].error == job.error
