"""Unit tests for jobs API validation and authentication."""

from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import pytest
from computegrid_api.api.auth import AuthenticatedClient, authenticate_client
from computegrid_api.api.jobs import create_job
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.config import Settings
from computegrid_api.db.models import Job, JobState
from computegrid_api.jobs.repository import (
    CreatedJob,
    IdempotencyConflictError,
    JobRepository,
)
from computegrid_api.main import app
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr, ValidationError

VALID_REQUEST = {
    "type": "monte_carlo_pi",
    "parameters": {"samples": 1000, "seed": 42},
}


@dataclass
class StubRepository:
    """Minimal configurable repository used by route unit tests."""

    create_result: CreatedJob | None = None
    create_error: Exception | None = None

    def create(self, **_: object) -> CreatedJob:
        if self.create_error is not None:
            raise self.create_error
        assert self.create_result is not None
        return self.create_result


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        development_client_id="unit-client",
    )


@pytest.fixture
def stub_repository() -> StubRepository:
    job = Job(id=uuid4(), state=JobState.ACCEPTED)
    return StubRepository(create_result=CreatedJob(job=job, created=True))


def test_create_job_returns_accepted(stub_repository: StubRepository) -> None:
    response = create_job(
        request=JobCreateRequest.model_validate(VALID_REQUEST),
        idempotency_key="request-1",
        client=AuthenticatedClient(client_id="unit-client"),
        repository=cast(JobRepository, stub_repository),
    )

    assert stub_repository.create_result is not None
    assert response.job_id == stub_repository.create_result.job.id
    assert response.state == JobState.ACCEPTED
    assert response.status_url == f"/v1/jobs/{response.job_id}"


@pytest.mark.parametrize("credentials", [None, "wrong-token"])
def test_authentication_rejects_missing_or_invalid_token(
    credentials: str | None, settings: Settings
) -> None:
    authorization = (
        None
        if credentials is None
        else HTTPAuthorizationCredentials(scheme="Bearer", credentials=credentials)
    )

    with pytest.raises(HTTPException) as raised:
        authenticate_client(authorization, settings)

    assert raised.value.status_code == 401
    assert raised.value.headers == {"WWW-Authenticate": "Bearer"}


def test_authentication_returns_configured_client(settings: Settings) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")

    client = authenticate_client(credentials, settings)

    assert client.client_id == "unit-client"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unsupported", "parameters": {"samples": 1000, "seed": 42}},
        {"type": "monte_carlo_pi", "parameters": {"samples": 0, "seed": 42}},
        {
            "type": "monte_carlo_pi",
            "parameters": {"samples": 1000, "seed": 42, "unexpected": True},
        },
    ],
)
def test_create_job_validates_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate(payload)


def test_openapi_requires_idempotency_key() -> None:
    operation = cast(dict[str, Any], app.openapi()["paths"]["/v1/jobs"]["post"])
    header = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "Idempotency-Key"
    )

    assert header["in"] == "header"
    assert header["required"] is True


def test_conflicting_idempotency_key_returns_conflict(
    stub_repository: StubRepository,
) -> None:
    stub_repository.create_error = IdempotencyConflictError()

    with pytest.raises(HTTPException) as raised:
        create_job(
            request=JobCreateRequest.model_validate(VALID_REQUEST),
            idempotency_key="request-1",
            client=AuthenticatedClient(client_id="unit-client"),
            repository=cast(JobRepository, stub_repository),
        )

    assert raised.value.status_code == 409
