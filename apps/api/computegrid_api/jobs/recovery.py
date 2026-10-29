"""Recovery of worker attempts whose durable execution leases expired."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from random import random

from sqlalchemy import select

from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState
from computegrid_api.db.session import SessionFactory, transaction
from computegrid_api.jobs.execution import (
    ExecutionDisposition,
    RandomSource,
    RetryPolicy,
    apply_attempt_failure,
    configured_retry_policy,
)


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Counts produced by one bounded lease-recovery pass."""

    recovered: int = 0
    retries_scheduled: int = 0
    terminal_failures: int = 0
    stale_attempts: int = 0


def recover_expired_leases(
    session_factory: SessionFactory,
    *,
    batch_size: int,
    now: datetime | None = None,
    retry_policy: RetryPolicy | None = None,
    random_source: RandomSource = random,
) -> RecoverySummary:
    """Fence and recover a locked batch of expired RUNNING attempts."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    recovery_time = now or datetime.now(UTC)
    if recovery_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    policy = retry_policy or configured_retry_policy()
    recovered = 0
    retries_scheduled = 0
    terminal_failures = 0
    stale_attempts = 0

    with transaction(session_factory) as session:
        candidates = list(
            session.execute(
                select(JobAttempt.id, JobAttempt.job_id)
                .where(
                    JobAttempt.state == AttemptState.RUNNING,
                    JobAttempt.lease_expires_at.is_not(None),
                    JobAttempt.lease_expires_at <= recovery_time,
                )
                .order_by(JobAttempt.lease_expires_at, JobAttempt.id)
                .limit(batch_size)
            )
        )
        for attempt_id, job_id in candidates:
            job = session.scalar(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            if job is None:
                continue
            attempt = session.scalar(
                select(JobAttempt)
                .where(
                    JobAttempt.id == attempt_id,
                    JobAttempt.state == AttemptState.RUNNING,
                    JobAttempt.lease_expires_at.is_not(None),
                    JobAttempt.lease_expires_at <= recovery_time,
                )
                .with_for_update(skip_locked=True)
            )
            if attempt is None:
                continue

            if job.state != JobState.RUNNING or job.version != attempt.fencing_token:
                attempt.state = AttemptState.ABANDONED
                attempt.lease_expires_at = None
                attempt.finished_at = recovery_time
                attempt.error = {
                    "type": "StaleAttempt",
                    "message": "attempt no longer owns the job fencing token",
                }
                stale_attempts += 1
                continue

            job.version += 1
            disposition = apply_attempt_failure(
                session=session,
                job=job,
                attempt=attempt,
                error_payload={
                    "type": "WorkerLeaseExpired",
                    "message": "worker did not complete before its durable lease expired",
                    "worker_id": attempt.worker_id,
                    "lease_expired_at": attempt.lease_expires_at.isoformat()
                    if attempt.lease_expires_at is not None
                    else None,
                },
                retryable=True,
                retry_policy=policy,
                random_source=random_source,
                now=recovery_time,
                attempt_state=AttemptState.ABANDONED,
            )
            recovered += 1
            if disposition == ExecutionDisposition.RETRY_SCHEDULED:
                retries_scheduled += 1
            else:
                terminal_failures += 1

    return RecoverySummary(
        recovered=recovered,
        retries_scheduled=retries_scheduled,
        terminal_failures=terminal_failures,
        stale_attempts=stale_attempts,
    )
