"""Idempotent execution and durable state transitions for allowlisted jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from random import Random
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState
from computegrid_api.db.session import SessionFactory, get_session_factory, transaction

type JobResult = dict[str, Any]
type Computation = Callable[[dict[str, Any]], JobResult]


class ExecutionDisposition(StrEnum):
    """Outcome used by the Celery adapter to decide message acknowledgement."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    IN_PROGRESS = "IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Fenced ownership of one physical execution attempt."""

    attempt_id: UUID
    fencing_token: int
    parameters: dict[str, Any]


def monte_carlo_pi(parameters: dict[str, Any]) -> JobResult:
    """Estimate pi reproducibly from a fixed sample count and seed."""
    samples = int(parameters["samples"])
    seed = int(parameters["seed"])
    random = Random(seed)
    inside = sum(
        1
        for _ in range(samples)
        if (x := random.random()) * x + (y := random.random()) * y <= 1.0
    )
    return {
        "pi_estimate": 4.0 * inside / samples,
        "samples": samples,
        "seed": seed,
    }


def execute_job(
    job_id: UUID,
    *,
    worker_id: str,
    session_factory: SessionFactory | None = None,
    computation: Computation = monte_carlo_pi,
) -> ExecutionDisposition:
    """Claim, execute, and durably finish one job without retry policy."""
    factory = session_factory or get_session_factory()
    claim = _claim_job(job_id, worker_id=worker_id, session_factory=factory)
    if isinstance(claim, ExecutionDisposition):
        return claim

    try:
        result = computation(claim.parameters)
    except Exception as error:
        _finish_failed(
            job_id,
            claim,
            error=error,
            session_factory=factory,
        )
        return ExecutionDisposition.FAILED

    _finish_succeeded(job_id, claim, result=result, session_factory=factory)
    return ExecutionDisposition.COMPLETED


def _claim_job(
    job_id: UUID, *, worker_id: str, session_factory: SessionFactory
) -> JobClaim | ExecutionDisposition:
    now = datetime.now(UTC)
    with transaction(session_factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"job {job_id} does not exist")
        if job.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            return ExecutionDisposition.ALREADY_TERMINAL
        if job.state == JobState.RUNNING:
            return ExecutionDisposition.IN_PROGRESS
        if job.state not in {JobState.ACCEPTED, JobState.QUEUED}:
            return ExecutionDisposition.IN_PROGRESS

        attempt_number = (
            session.scalar(
                select(func.coalesce(func.max(JobAttempt.attempt_number), 0)).where(
                    JobAttempt.job_id == job.id
                )
            )
            or 0
        ) + 1
        fencing_token = job.version + 1
        attempt = JobAttempt(
            id=uuid4(),
            job_id=job.id,
            attempt_number=attempt_number,
            fencing_token=fencing_token,
            state=AttemptState.RUNNING,
            worker_id=worker_id,
            started_at=now,
        )
        session.add(attempt)
        job.state = JobState.RUNNING
        job.started_at = job.started_at or now
        job.version = fencing_token
        job.error = None
        session.flush()
        return JobClaim(
            attempt_id=attempt.id,
            fencing_token=fencing_token,
            parameters=dict(job.parameters),
        )


def _finish_succeeded(
    job_id: UUID,
    claim: JobClaim,
    *,
    result: JobResult,
    session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with transaction(session_factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        attempt = session.get(JobAttempt, claim.attempt_id, with_for_update=True)
        if job is None or attempt is None:
            raise RuntimeError("claimed job or attempt no longer exists")
        if job.state != JobState.RUNNING or job.version != claim.fencing_token:
            raise RuntimeError("execution claim is stale")

        job.state = JobState.SUCCEEDED
        job.result = result
        job.error = None
        job.completed_at = now
        attempt.state = AttemptState.SUCCEEDED
        attempt.finished_at = now


def _finish_failed(
    job_id: UUID,
    claim: JobClaim,
    *,
    error: Exception,
    session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    error_payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error)[:1000],
    }
    with transaction(session_factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        attempt = session.get(JobAttempt, claim.attempt_id, with_for_update=True)
        if job is None or attempt is None:
            raise RuntimeError("claimed job or attempt no longer exists") from error
        if job.state != JobState.RUNNING or job.version != claim.fencing_token:
            raise RuntimeError("execution claim is stale") from error

        job.state = JobState.FAILED
        job.result = None
        job.error = error_payload
        job.completed_at = now
        attempt.state = AttemptState.FAILED
        attempt.error = error_payload
        attempt.finished_at = now
