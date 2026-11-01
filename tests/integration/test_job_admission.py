"""PostgreSQL integration coverage for shared admission and retention cleanup."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from computegrid_api import compute_demo
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.compute_demo import advance_feeds, start_run
from computegrid_api.config import ComputeDemoSettings
from computegrid_api.db.models import Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory
from computegrid_api.jobs import repository as repository_module
from computegrid_api.jobs.repository import (
    DemoCapacityExceededError,
    JobCreationLimits,
    JobRepository,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


@pytest.fixture
def job_request() -> JobCreateRequest:
    return JobCreateRequest.model_validate(
        {"type": "monte_carlo_pi", "parameters": {"samples": 100, "seed": 42}}
    )


@pytest.fixture
def policy() -> ComputeDemoSettings:
    return ComputeDemoSettings(  # type: ignore[call-arg]
        _env_file=None,
        compute_demo_interval_seconds=0.5,
        compute_demo_max_jobs=4,
        compute_demo_max_feeds=1,
        compute_demo_active_capacity=10,
        compute_demo_retained_capacity=2,
        compute_demo_cooldown_seconds=120,
        compute_demo_samples=100,
        compute_demo_variables=4,
    )


def limits(*, retained: int = 2) -> JobCreationLimits:
    return JobCreationLimits(
        max_active_per_client=10,
        max_active_global=10,
        max_retained_per_client=retained,
        max_retained_global=retained,
    )


def finish_and_publish(
    factory: SessionFactory,
    job_id: UUID,
    *,
    created_at: datetime | None = None,
) -> None:
    with factory.begin() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.state = JobState.FAILED
        job.completed_at = datetime.now(UTC)
        if created_at is not None:
            job.created_at = created_at
        for event in session.scalars(select(OutboxEvent).where(OutboxEvent.job_id == job.id)):
            event.published_at = datetime.now(UTC)


def test_individual_cleanup_evicts_oldest_eligible_job(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
) -> None:
    repository = JobRepository(session_factory)
    first = repository.create(client_id="client", idempotency_key="first", request=job_request)
    second = repository.create(client_id="client", idempotency_key="second", request=job_request)
    now = datetime.now(UTC)
    finish_and_publish(session_factory, first.job.id, created_at=now - timedelta(minutes=2))
    finish_and_publish(session_factory, second.job.id, created_at=now - timedelta(minutes=1))

    third = repository.create(
        client_id="client",
        idempotency_key="third",
        request=job_request,
        limits=limits(),
    )

    with session_factory() as session:
        ids = set(session.scalars(select(Job.id)))
        assert ids == {second.job.id, third.job.id}
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2


def test_individual_quota_excludes_run_demo_jobs_and_protects_active_run(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
    policy: ComputeDemoSettings,
) -> None:
    repository = JobRepository(session_factory)
    now = datetime.now(UTC)
    old = repository.create(
        client_id="old-client", idempotency_key="old", request=job_request
    )
    finish_and_publish(session_factory, old.job.id, created_at=now - timedelta(minutes=3))
    run_id = start_run(session_factory, "same-client", "run", policy, now=now)
    assert advance_feeds(session_factory, policy, now=now) == 1
    with session_factory() as session:
        demo_job_id = session.scalar(select(Job.id).where(Job.demo_run_id == run_id))
    assert demo_job_id is not None
    finish_and_publish(session_factory, demo_job_id, created_at=now - timedelta(minutes=2))

    individual = repository.create(
        client_id="same-client",
        idempotency_key="individual",
        request=job_request,
        limits=JobCreationLimits(1, 10, 1, 2),
    )

    with session_factory() as session:
        assert session.get(Job, old.job.id) is None
        assert session.get(Job, demo_job_id) is not None
        assert session.get(Job, individual.job.id) is not None


def test_run_demo_uses_shared_cleanup(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
    policy: ComputeDemoSettings,
) -> None:
    repository = JobRepository(session_factory)
    old = repository.create(
        client_id="individual", idempotency_key="old", request=job_request
    )
    finish_and_publish(session_factory, old.job.id)
    now = datetime.now(UTC)
    start_run(session_factory, "demo", "run", policy, now=now)
    policy.compute_demo_retained_capacity = 1

    assert advance_feeds(session_factory, policy, now=now) == 1
    with session_factory() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        assert jobs[0].demo_run_id is not None


def test_unpublished_terminal_job_is_not_evicted(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="client", idempotency_key="retained", request=job_request
    )
    with session_factory.begin() as session:
        job = session.get(Job, retained.job.id)
        assert job is not None
        job.state = JobState.FAILED

    with pytest.raises(DemoCapacityExceededError):
        repository.create(
            client_id="other",
            idempotency_key="replacement",
            request=job_request,
            limits=limits(retained=1),
        )


def test_active_job_is_not_evicted(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="client", idempotency_key="active", request=job_request
    )

    with pytest.raises(DemoCapacityExceededError):
        repository.create(
            client_id="other",
            idempotency_key="replacement",
            request=job_request,
            limits=limits(retained=1),
        )

    with session_factory() as session:
        assert session.get(Job, retained.job.id) is not None


def test_idempotent_retry_wins_before_retention_cleanup(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="client", idempotency_key="same-request", request=job_request
    )
    finish_and_publish(session_factory, retained.job.id)

    retry = repository.create(
        client_id="client",
        idempotency_key="same-request",
        request=job_request,
        limits=limits(retained=1),
    )

    assert not retry.created
    assert retry.job.id == retained.job.id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_cleanup_and_replacement_roll_back_together(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="client", idempotency_key="retained", request=job_request
    )
    finish_and_publish(session_factory, retained.job.id)

    def invalid_event(job: Job) -> OutboxEvent:
        return OutboxEvent(
            id=uuid4(),
            job_id=uuid4(),
            event_type="job.accepted",
            payload={"job_id": str(job.id)},
        )

    monkeypatch.setattr(repository_module, "build_job_accepted_event", invalid_event)
    with pytest.raises(IntegrityError):
        repository.create(
            client_id="other",
            idempotency_key="replacement",
            request=job_request,
            limits=limits(retained=1),
        )

    with session_factory() as session:
        assert session.get(Job, retained.job.id) is not None
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_concurrent_retention_admission_accepts_only_one_replacement(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="old", idempotency_key="retained", request=job_request
    )
    finish_and_publish(session_factory, retained.job.id)
    barrier = Barrier(2)

    def submit(index: int) -> str:
        barrier.wait()
        try:
            repository.create(
                client_id=f"client-{index}",
                idempotency_key=f"replacement-{index}",
                request=job_request,
                limits=limits(retained=1),
            )
        except DemoCapacityExceededError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, range(2)))

    assert sorted(outcomes) == ["accepted", "rejected"]


def test_run_demo_cleanup_rolls_back_with_cursor(
    session_factory: SessionFactory,
    job_request: JobCreateRequest,
    policy: ComputeDemoSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = JobRepository(session_factory)
    retained = repository.create(
        client_id="old", idempotency_key="retained", request=job_request
    )
    finish_and_publish(session_factory, retained.job.id)
    now = datetime.now(UTC)
    run_id = start_run(session_factory, "demo", "run", policy, now=now)
    policy.compute_demo_retained_capacity = 1

    def invalid_event(job: Job) -> OutboxEvent:
        return OutboxEvent(
            job_id=job.id,
            event_type="job.accepted",
            payload={},
            publish_attempts=-1,
        )

    monkeypatch.setattr(compute_demo, "build_job_accepted_event", invalid_event)
    with pytest.raises(IntegrityError):
        advance_feeds(session_factory, policy, now=now)

    with session_factory() as session:
        assert session.get(Job, retained.job.id) is not None
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(Job.id).where(Job.demo_run_id == run_id)) is None
