"""Idempotent execution and durable state transitions for allowlisted jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from random import Random, random
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from computegrid_api.config import get_lease_settings, get_retry_settings
from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, get_session_factory, transaction
from computegrid_api.jobs.optimization import weighted_max_cut

type JobResult = dict[str, Any]
type Computation = Callable[[dict[str, Any]], JobResult]
type RandomSource = Callable[[], float]


class RetryableExecutionError(Exception):
    """A workload failure that may succeed on a later durable attempt."""


class StaleExecutionError(RuntimeError):
    """A fenced worker attempted to commit after losing ownership."""


class ExecutionDisposition(StrEnum):
    """Outcome used by the Celery adapter to decide message acknowledgement."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    SUPERSEDED = "SUPERSEDED"
    IN_PROGRESS = "IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Fenced ownership of one physical execution attempt."""

    attempt_id: UUID
    attempt_number: int
    fencing_token: int
    job_type: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Maximum attempts and bounded exponential backoff with proportional jitter."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_ratio: float

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_seconds(self, attempt_number: int, *, random_value: float) -> float:
        """Return the delay before the attempt following ``attempt_number``."""
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between 0 and 1")
        exponential_cap = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2.0 ** (attempt_number - 1)),
        )
        minimum = exponential_cap * (1 - self.jitter_ratio)
        return minimum + (exponential_cap - minimum) * random_value


def configured_retry_policy() -> RetryPolicy:
    """Build the immutable worker retry policy from environment settings."""
    settings = get_retry_settings()
    return RetryPolicy(
        max_attempts=settings.job_max_attempts,
        base_delay_seconds=settings.retry_base_delay_seconds,
        max_delay_seconds=settings.retry_max_delay_seconds,
        jitter_ratio=settings.retry_jitter_ratio,
    )


def configured_lease_duration() -> timedelta:
    """Return the durable lease duration configured for worker claims."""
    return timedelta(seconds=get_lease_settings().job_lease_seconds)


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


def computation_for(job_type: str) -> Computation:
    """Return the allowlisted implementation for one persisted job type."""
    computations: dict[str, Computation] = {
        "monte_carlo_pi": monte_carlo_pi,
        "weighted_max_cut": weighted_max_cut,
    }
    try:
        return computations[job_type]
    except KeyError as error:
        raise ValueError(f"unsupported persisted job type: {job_type}") from error


def execute_job(
    job_id: UUID,
    *,
    expected_attempt_number: int = 1,
    worker_id: str,
    session_factory: SessionFactory | None = None,
    computation: Computation | None = None,
    retry_policy: RetryPolicy | None = None,
    random_source: RandomSource = random,
    lease_duration: timedelta | None = None,
) -> ExecutionDisposition:
    """Claim, execute, and durably finish or reschedule one job attempt."""
    factory = session_factory or get_session_factory()
    policy = retry_policy or configured_retry_policy()
    active_lease_duration = (
        lease_duration if lease_duration is not None else configured_lease_duration()
    )
    if active_lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    claim = _claim_job(
        job_id,
        expected_attempt_number=expected_attempt_number,
        worker_id=worker_id,
        lease_duration=active_lease_duration,
        session_factory=factory,
    )
    if isinstance(claim, ExecutionDisposition):
        return claim

    try:
        active_computation = computation or computation_for(claim.job_type)
        result = active_computation(claim.parameters)
    except Exception as error:
        return _finish_failure(
            job_id,
            claim,
            error=error,
            retry_policy=policy,
            random_source=random_source,
            session_factory=factory,
        )

    _finish_succeeded(job_id, claim, result=result, session_factory=factory)
    return ExecutionDisposition.COMPLETED


def _claim_job(
    job_id: UUID,
    *,
    expected_attempt_number: int,
    worker_id: str,
    lease_duration: timedelta,
    session_factory: SessionFactory,
) -> JobClaim | ExecutionDisposition:
    if expected_attempt_number < 1:
        raise ValueError("expected_attempt_number must be positive")
    now = datetime.now(UTC)
    with transaction(session_factory) as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise RuntimeError(f"job {job_id} does not exist")
        if job.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            return ExecutionDisposition.ALREADY_TERMINAL
        latest_attempt_number = (
            session.scalar(
                select(func.coalesce(func.max(JobAttempt.attempt_number), 0)).where(
                    JobAttempt.job_id == job.id
                )
            )
            or 0
        )
        if job.state == JobState.RUNNING:
            if expected_attempt_number < latest_attempt_number:
                return ExecutionDisposition.SUPERSEDED
            return ExecutionDisposition.IN_PROGRESS

        next_attempt_number = latest_attempt_number + 1
        if expected_attempt_number < next_attempt_number:
            return ExecutionDisposition.SUPERSEDED
        if expected_attempt_number > next_attempt_number:
            return ExecutionDisposition.IN_PROGRESS
        if job.state not in {JobState.ACCEPTED, JobState.QUEUED, JobState.RETRY_WAIT}:
            return ExecutionDisposition.IN_PROGRESS

        fencing_token = job.version + 1
        attempt = JobAttempt(
            id=uuid4(),
            job_id=job.id,
            attempt_number=expected_attempt_number,
            fencing_token=fencing_token,
            state=AttemptState.RUNNING,
            worker_id=worker_id,
            lease_expires_at=now + lease_duration,
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
            attempt_number=expected_attempt_number,
            fencing_token=fencing_token,
            job_type=job.job_type,
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
            raise StaleExecutionError("execution claim is stale")

        job.state = JobState.SUCCEEDED
        job.result = result
        job.error = None
        job.completed_at = now
        attempt.state = AttemptState.SUCCEEDED
        attempt.lease_expires_at = None
        attempt.finished_at = now


def _finish_failure(
    job_id: UUID,
    claim: JobClaim,
    *,
    error: Exception,
    retry_policy: RetryPolicy,
    random_source: RandomSource,
    session_factory: SessionFactory,
) -> ExecutionDisposition:
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
            raise StaleExecutionError("execution claim is stale") from error

        return apply_attempt_failure(
            session=session,
            job=job,
            attempt=attempt,
            error_payload=error_payload,
            retryable=isinstance(error, RetryableExecutionError),
            retry_policy=retry_policy,
            random_source=random_source,
            now=now,
            attempt_state=AttemptState.FAILED,
        )


def apply_attempt_failure(
    *,
    session: Session,
    job: Job,
    attempt: JobAttempt,
    error_payload: dict[str, Any],
    retryable: bool,
    retry_policy: RetryPolicy,
    random_source: RandomSource,
    now: datetime,
    attempt_state: AttemptState,
) -> ExecutionDisposition:
    """Persist one failed/abandoned attempt and its next durable state."""
    exhausted = attempt.attempt_number >= retry_policy.max_attempts
    retry_at: datetime | None = None
    if retryable:
        error_payload.update(
            {
                "retryable": True,
                "attempt_number": attempt.attempt_number,
                "max_attempts": retry_policy.max_attempts,
            }
        )
        if exhausted:
            error_payload["attempts_exhausted"] = True
        else:
            delay = retry_policy.delay_seconds(
                attempt.attempt_number,
                random_value=random_source(),
            )
            retry_at = now + timedelta(seconds=delay)
            error_payload["retry_at"] = retry_at.isoformat()

    job.result = None
    job.error = error_payload
    attempt.state = attempt_state
    attempt.error = error_payload
    attempt.lease_expires_at = None
    attempt.finished_at = now

    if retry_at is not None:
        job.state = JobState.RETRY_WAIT
        job.completed_at = None
        session.add(
            OutboxEvent(
                id=uuid4(),
                job_id=job.id,
                event_type="job.retry_scheduled",
                payload={
                    "job_id": str(job.id),
                    "attempt_number": attempt.attempt_number + 1,
                    "previous_attempt_number": attempt.attempt_number,
                },
                next_attempt_at=retry_at,
            )
        )
        return ExecutionDisposition.RETRY_SCHEDULED

    job.state = JobState.FAILED
    job.completed_at = now
    return ExecutionDisposition.FAILED
