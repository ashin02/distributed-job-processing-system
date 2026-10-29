"""PostgreSQL integration tests for job submission and retrieval."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from computegrid_api.api.auth import AuthenticatedClient
from computegrid_api.api.jobs import create_job, get_job, list_jobs
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.models import Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory
from computegrid_api.jobs import repository as repository_module
from computegrid_api.jobs.repository import JobRepository
from fastapi import HTTPException
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

VALID_REQUEST = {
    "type": "monte_carlo_pi",
    "parameters": {"samples": 1000, "seed": 42},
}


@pytest.fixture
def repository(session_factory: SessionFactory) -> JobRepository:
    return JobRepository(session_factory)


def test_submit_get_and_list_jobs(repository: JobRepository, migrated_engine: Engine) -> None:
    client = AuthenticatedClient(client_id="integration-client")
    request = JobCreateRequest.model_validate(VALID_REQUEST)
    first_response = create_job(
        request=request,
        idempotency_key="request-1",
        client=client,
        repository=repository,
    )
    second_response = create_job(
        request=request,
        idempotency_key="request-2",
        client=client,
        repository=repository,
    )

    assert first_response.state == JobState.ACCEPTED
    assert second_response.state == JobState.ACCEPTED

    get_response = get_job(
        job_id=first_response.job_id,
        client=client,
        repository=repository,
    )
    assert get_response.attempts == []
    assert get_response.result is None

    page = list_jobs(client=client, repository=repository, limit=1, offset=0)
    assert page.total == 2
    assert page.limit == 1
    assert len(page.items) == 1

    with migrated_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == 2


def test_identical_retry_returns_one_job_and_event(
    repository: JobRepository, migrated_engine: Engine
) -> None:
    client = AuthenticatedClient(client_id="integration-client")
    request = JobCreateRequest.model_validate(VALID_REQUEST)
    first = create_job(
        request=request,
        idempotency_key="same-request",
        client=client,
        repository=repository,
    )
    retry = create_job(
        request=request,
        idempotency_key="same-request",
        client=client,
        repository=repository,
    )

    assert first == retry
    with migrated_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Job)) == 1
        assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_conflicting_retry_returns_conflict(repository: JobRepository) -> None:
    client = AuthenticatedClient(client_id="integration-client")
    create_job(
        request=JobCreateRequest.model_validate(VALID_REQUEST),
        idempotency_key="conflict",
        client=client,
        repository=repository,
    )

    with pytest.raises(HTTPException) as raised:
        create_job(
            request=JobCreateRequest.model_validate(
                {
                    "type": "monte_carlo_pi",
                    "parameters": {"samples": 2000, "seed": 42},
                }
            ),
            idempotency_key="conflict",
            client=client,
            repository=repository,
        )

    assert raised.value.status_code == 409


def test_jobs_are_scoped_to_client(repository: JobRepository) -> None:
    request = JobCreateRequest.model_validate(VALID_REQUEST)
    first = repository.create(client_id="client-a", idempotency_key="shared", request=request)
    second = repository.create(client_id="client-b", idempotency_key="shared", request=request)

    assert first.job.id != second.job.id
    assert repository.get(client_id="client-b", job_id=first.job.id) is None
    assert repository.list(client_id="client-a", limit=20, offset=0).total == 1
    assert repository.list(client_id="client-b", limit=20, offset=0).total == 1


def test_concurrent_identical_submissions_create_one_job(
    repository: JobRepository, migrated_engine: Engine
) -> None:
    request = JobCreateRequest.model_validate(VALID_REQUEST)
    submissions = 8
    barrier = Barrier(submissions)

    def submit() -> tuple[str, bool]:
        barrier.wait()
        result = repository.create(
            client_id="concurrent-client",
            idempotency_key="concurrent-request",
            request=request,
        )
        return str(result.job.id), result.created

    with ThreadPoolExecutor(max_workers=submissions) as executor:
        results = list(executor.map(lambda _: submit(), range(submissions)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with migrated_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Job)) == 1
        assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_outbox_failure_rolls_back_job(
    repository: JobRepository,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_event(job: Job) -> OutboxEvent:
        return OutboxEvent(
            id=uuid4(),
            job_id=uuid4(),
            event_type="job.accepted",
            payload={"job_id": str(job.id)},
        )

    monkeypatch.setattr(repository_module, "build_job_accepted_event", invalid_event)
    request = JobCreateRequest.model_validate(VALID_REQUEST)

    with pytest.raises(IntegrityError):
        repository.create(
            client_id="rollback-client",
            idempotency_key="rollback-request",
            request=request,
        )

    with migrated_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Job)) == 0
        assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert (
            connection.scalar(
                select(func.count()).select_from(Job).where(Job.state != JobState.ACCEPTED)
            )
            == 0
        )
