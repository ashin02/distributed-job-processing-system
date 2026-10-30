"""PostgreSQL integration coverage for shared anonymous-demo protections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.session import SessionFactory
from computegrid_api.demo_access import (
    DemoAccessService,
    DemoRateLimit,
    DemoRateLimitExceededError,
)
from computegrid_api.jobs.repository import (
    DemoCapacityExceededError,
    JobCreationLimits,
    JobRepository,
)

pytestmark = pytest.mark.integration


def test_demo_rate_limit_is_shared_in_postgresql(session_factory: SessionFactory) -> None:
    service = DemoAccessService(session_factory)
    budget = DemoRateLimit(scope="test-reads", limit=2, window_seconds=60)

    service.check(subject="visitor-a", budget=budget)
    service.check(subject="visitor-a", budget=budget)

    with pytest.raises(DemoRateLimitExceededError):
        service.check(subject="visitor-a", budget=budget)

    service.check(subject="visitor-b", budget=budget)


def test_demo_active_job_capacity_and_idempotency(session_factory: SessionFactory) -> None:
    repository = JobRepository(session_factory)
    request = JobCreateRequest.model_validate(
        {"type": "monte_carlo_pi", "parameters": {"samples": 100, "seed": 42}}
    )
    limits = JobCreationLimits(
        max_active_per_client=1,
        max_active_global=10,
        max_retained_per_client=10,
        max_retained_global=100,
    )

    first = repository.create(
        client_id="demo-a", idempotency_key="one", request=request, limits=limits
    )
    retry = repository.create(
        client_id="demo-a", idempotency_key="one", request=request, limits=limits
    )

    assert retry.job.id == first.job.id
    with pytest.raises(DemoCapacityExceededError):
        repository.create(
            client_id="demo-a", idempotency_key="two", request=request, limits=limits
        )


def test_demo_visitor_cannot_read_another_visitors_job(
    session_factory: SessionFactory,
) -> None:
    repository = JobRepository(session_factory)
    request = JobCreateRequest.model_validate(
        {"type": "monte_carlo_pi", "parameters": {"samples": 100, "seed": 7}}
    )
    created = repository.create(
        client_id="demo-owner", idempotency_key="private", request=request
    )

    assert repository.get(client_id="demo-owner", job_id=created.job.id) is not None
    assert repository.get(client_id="demo-other", job_id=created.job.id) is None


def test_global_active_capacity_is_safe_under_concurrent_submissions(
    session_factory: SessionFactory,
) -> None:
    repository = JobRepository(session_factory)
    request = JobCreateRequest.model_validate(
        {"type": "monte_carlo_pi", "parameters": {"samples": 100, "seed": 9}}
    )
    limits = JobCreationLimits(
        max_active_per_client=2,
        max_active_global=1,
        max_retained_per_client=10,
        max_retained_global=100,
    )
    barrier = Barrier(2)

    def submit(index: int) -> str:
        barrier.wait()
        try:
            repository.create(
                client_id=f"demo-{index}",
                idempotency_key=f"request-{index}",
                request=request,
                limits=limits,
            )
        except DemoCapacityExceededError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, range(2)))

    assert sorted(outcomes) == ["accepted", "rejected"]
