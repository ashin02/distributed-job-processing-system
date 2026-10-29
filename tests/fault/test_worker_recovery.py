"""Fault injection for crashed workers, expired leases, and fencing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, transaction
from computegrid_api.jobs.execution import (
    ExecutionDisposition,
    RetryPolicy,
    StaleExecutionError,
    execute_job,
)
from computegrid_api.jobs.recovery import RecoverySummary, recover_expired_leases
from computegrid_api.jobs.repository import JobRepository
from computegrid_api.outbox_relay import relay_once
from sqlalchemy import select

pytestmark = pytest.mark.fault


def create_job(session_factory: SessionFactory, key: str) -> UUID:
    created = JobRepository(session_factory).create(
        client_id="fault-client",
        idempotency_key=key,
        request=JobCreateRequest.model_validate(
            {
                "type": "monte_carlo_pi",
                "parameters": {"samples": 1_000, "seed": 42},
            }
        ),
    )
    return created.job.id


def retry_policy(max_attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=1,
        max_delay_seconds=10,
        jitter_ratio=0,
    )


@dataclass
class RecordingPublisher:
    published: list[UUID] = field(default_factory=list)

    def publish(self, event: OutboxEvent) -> None:
        self.published.append(event.id)


def inject_running_attempt(
    session_factory: SessionFactory,
    job_id: UUID,
    *,
    lease_expires_at: datetime,
) -> UUID:
    attempt_id = uuid4()
    with transaction(session_factory) as session:
        job = session.get(Job, job_id, with_for_update=True)
        assert job is not None
        job.state = JobState.RUNNING
        job.version = 1
        job.started_at = lease_expires_at - timedelta(seconds=5)
        session.add(
            JobAttempt(
                id=attempt_id,
                job_id=job.id,
                attempt_number=1,
                fencing_token=1,
                state=AttemptState.RUNNING,
                worker_id="crashed-worker",
                started_at=job.started_at,
                lease_expires_at=lease_expires_at,
            )
        )
    return attempt_id


def test_expired_lease_fences_stale_worker_and_recovers_duplicate_safely(
    session_factory: SessionFactory,
) -> None:
    job_id = create_job(session_factory, "stale-worker")
    started = Event()
    release = Event()
    policy = retry_policy()

    def stalled_computation(_: dict[str, object]) -> dict[str, object]:
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("fault-injection worker was not released")
        return {"pi_estimate": 99.0, "samples": 1_000, "seed": 42}

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_worker = executor.submit(
            execute_job,
            job_id,
            expected_attempt_number=1,
            worker_id="worker-that-crashes",
            session_factory=session_factory,
            computation=stalled_computation,
            retry_policy=policy,
            lease_duration=timedelta(seconds=1),
        )
        assert started.wait(timeout=5)

        with session_factory() as session:
            attempt = session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            assert attempt is not None
            assert attempt.lease_expires_at is not None
            recovery_time = attempt.lease_expires_at + timedelta(seconds=1)

        assert (
            execute_job(
                job_id,
                expected_attempt_number=1,
                worker_id="redelivered-while-running",
                session_factory=session_factory,
                retry_policy=policy,
            )
            == ExecutionDisposition.IN_PROGRESS
        )

        try:
            summary = recover_expired_leases(
                session_factory,
                batch_size=10,
                now=recovery_time,
                retry_policy=policy,
                random_source=lambda: 0.0,
            )
        finally:
            release.set()

        assert summary == RecoverySummary(recovered=1, retries_scheduled=1)
        with pytest.raises(StaleExecutionError, match="claim is stale"):
            stale_worker.result(timeout=5)

    assert (
        recover_expired_leases(
            session_factory,
            batch_size=10,
            now=recovery_time,
            retry_policy=policy,
        )
        == RecoverySummary()
    )
    assert (
        execute_job(
            job_id,
            expected_attempt_number=1,
            worker_id="redelivered-after-recovery",
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

    assert relay_once(session_factory, RecordingPublisher(), batch_size=10) >= 1
    assert (
        execute_job(
            job_id,
            expected_attempt_number=2,
            worker_id="replacement-worker",
            session_factory=session_factory,
            retry_policy=policy,
        )
        == ExecutionDisposition.COMPLETED
    )

    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.SUCCEEDED
        assert job.result != {"pi_estimate": 99.0, "samples": 1_000, "seed": 42}
        assert [attempt.state for attempt in job.attempts] == [
            AttemptState.ABANDONED,
            AttemptState.SUCCEEDED,
        ]
        assert [attempt.fencing_token for attempt in job.attempts] == [1, 3]


def test_unexpired_running_lease_is_not_recovered(session_factory: SessionFactory) -> None:
    now = datetime.now(UTC)
    job_id = create_job(session_factory, "live-worker")
    attempt_id = inject_running_attempt(
        session_factory,
        job_id,
        lease_expires_at=now + timedelta(minutes=1),
    )

    assert recover_expired_leases(session_factory, batch_size=10, now=now) == RecoverySummary()

    with session_factory() as session:
        job = session.get(Job, job_id)
        attempt = session.get(JobAttempt, attempt_id)
        assert job is not None
        assert attempt is not None
        assert job.state == JobState.RUNNING
        assert attempt.state == AttemptState.RUNNING
        assert attempt.lease_expires_at is not None


def test_expired_final_attempt_becomes_terminal_failure(
    session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    job_id = create_job(session_factory, "exhausted-crash")
    attempt_id = inject_running_attempt(
        session_factory,
        job_id,
        lease_expires_at=now - timedelta(seconds=1),
    )

    summary = recover_expired_leases(
        session_factory,
        batch_size=10,
        now=now,
        retry_policy=retry_policy(max_attempts=1),
    )

    assert summary == RecoverySummary(recovered=1, terminal_failures=1)
    with session_factory() as session:
        job = session.get(Job, job_id)
        attempt = session.get(JobAttempt, attempt_id)
        retry_event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "job.retry_scheduled")
        )
        assert job is not None
        assert attempt is not None
        assert job.state == JobState.FAILED
        assert job.completed_at is not None
        assert job.error is not None
        assert job.error["type"] == "WorkerLeaseExpired"
        assert job.error["attempts_exhausted"] is True
        assert job.version == 2
        assert attempt.state == AttemptState.ABANDONED
        assert attempt.lease_expires_at is None
        assert retry_event is None
