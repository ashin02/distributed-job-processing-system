"""PostgreSQL integration tests for relay and execution state transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.models import AttemptState, Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory
from computegrid_api.jobs.execution import (
    ExecutionDisposition,
    RetryableExecutionError,
    RetryPolicy,
    execute_job,
)
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


def test_execution_dispatches_weighted_max_cut_job(
    session_factory: SessionFactory,
) -> None:
    created = JobRepository(session_factory).create(
        client_id="execution-client",
        idempotency_key="weighted-max-cut",
        request=JobCreateRequest.model_validate(
            {
                "type": "weighted_max_cut",
                "parameters": {
                    "nodes": ["a", "b", "c"],
                    "edges": [
                        {"source": "a", "target": "b", "weight": 2},
                        {"source": "b", "target": "c", "weight": 3},
                        {"source": "a", "target": "c", "weight": 1},
                    ],
                },
            }
        ),
    )

    assert (
        execute_job(
            created.job.id,
            worker_id="optimization-worker",
            session_factory=session_factory,
        )
        == ExecutionDisposition.COMPLETED
    )

    with session_factory() as session:
        job = session.get(Job, created.job.id)
        assert job is not None
        assert job.state == JobState.SUCCEEDED
        assert job.result is not None
        assert job.result["cut_weight"] == 5.0
        assert job.result["solver"]["optimal"] is True
        assert job.result["qubo"]["energy_relation"] == "energy = -cut_weight"


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
        retry_event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "job.retry_scheduled")
        )
        assert job is not None
        assert job.state == JobState.FAILED
        assert job.result is None
        assert job.error == {"type": "ValueError", "message": "invalid computation"}
        assert len(job.attempts) == 1
        assert job.attempts[0].state == AttemptState.FAILED
        assert job.attempts[0].error == job.error
        assert retry_event is None


def test_retryable_failure_waits_for_due_outbox_event_then_succeeds(
    session_factory: SessionFactory,
) -> None:
    job_id = create_job(session_factory)
    publisher = RecordingPublisher()
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=10,
        max_delay_seconds=60,
        jitter_ratio=0.5,
    )
    assert relay_once(session_factory, publisher, batch_size=10) == 1

    def fail_transiently(_: dict[str, object]) -> dict[str, object]:
        raise RetryableExecutionError("temporary dependency failure")

    assert (
        execute_job(
            job_id,
            expected_attempt_number=1,
            worker_id="worker-1",
            session_factory=session_factory,
            computation=fail_transiently,
            retry_policy=policy,
            random_source=lambda: 0.0,
        )
        == ExecutionDisposition.RETRY_SCHEDULED
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        retry_event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "job.retry_scheduled")
        )
        assert job is not None
        assert retry_event is not None
        assert job.state == JobState.RETRY_WAIT
        assert job.completed_at is None
        assert retry_event.payload["attempt_number"] == 2
        assert retry_event.next_attempt_at is not None
        assert retry_event.next_attempt_at > datetime.now(UTC)
        assert job.error is not None
        assert job.error["retryable"] is True
        assert job.error["retry_at"] == retry_event.next_attempt_at.isoformat()
        assert len(job.attempts) == 1
        assert job.attempts[0].state == AttemptState.FAILED

    assert relay_once(session_factory, publisher, batch_size=10) == 0
    assert (
        execute_job(
            job_id,
            expected_attempt_number=1,
            worker_id="duplicate-worker",
            session_factory=session_factory,
            retry_policy=policy,
        )
        == ExecutionDisposition.SUPERSEDED
    )

    with session_factory.begin() as session:
        retry_event = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "job.retry_scheduled")
            .with_for_update()
        )
        assert retry_event is not None
        retry_event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    assert relay_once(session_factory, publisher, batch_size=10) == 1
    assert (
        execute_job(
            job_id,
            expected_attempt_number=2,
            worker_id="worker-2",
            session_factory=session_factory,
            retry_policy=policy,
        )
        == ExecutionDisposition.COMPLETED
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.SUCCEEDED
        assert job.result == {"pi_estimate": 3.126, "samples": 10_000, "seed": 42}
        assert [attempt.attempt_number for attempt in job.attempts] == [1, 2]
        assert [attempt.state for attempt in job.attempts] == [
            AttemptState.FAILED,
            AttemptState.SUCCEEDED,
        ]


def test_retry_limit_transitions_to_inspectable_terminal_failure(
    session_factory: SessionFactory,
) -> None:
    job_id = create_job(session_factory)
    policy = RetryPolicy(
        max_attempts=2,
        base_delay_seconds=1,
        max_delay_seconds=10,
        jitter_ratio=0,
    )

    def fail_transiently(_: dict[str, object]) -> dict[str, object]:
        raise RetryableExecutionError("dependency still unavailable")

    assert (
        execute_job(
            job_id,
            expected_attempt_number=1,
            worker_id="worker-1",
            session_factory=session_factory,
            computation=fail_transiently,
            retry_policy=policy,
        )
        == ExecutionDisposition.RETRY_SCHEDULED
    )
    assert (
        execute_job(
            job_id,
            expected_attempt_number=2,
            worker_id="worker-2",
            session_factory=session_factory,
            computation=fail_transiently,
            retry_policy=policy,
        )
        == ExecutionDisposition.FAILED
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        retry_events = list(
            session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "job.retry_scheduled")
            )
        )
        assert job is not None
        assert job.state == JobState.FAILED
        assert job.completed_at is not None
        assert job.error is not None
        assert job.error["attempts_exhausted"] is True
        assert job.error["attempt_number"] == 2
        assert job.error["max_attempts"] == 2
        assert len(job.attempts) == 2
        assert len(retry_events) == 1
