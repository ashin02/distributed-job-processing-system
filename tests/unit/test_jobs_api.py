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
from computegrid_api.demo_access import DemoAccessService
from computegrid_api.jobs.repository import (
    CreatedJob,
    IdempotencyConflictError,
    JobRepository,
)
from computegrid_api.main import app
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import Response

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


class StubDemoAccessService:
    """No-op rate limiter for developer route calls."""

    def check(self, **_: object) -> None:
        return None


def http_request(cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = (
        [] if cookie is None else [(b"cookie", cookie.encode())]
    )
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
    }
    return Request(scope)


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


def test_create_job_returns_accepted(
    stub_repository: StubRepository, settings: Settings
) -> None:
    response = create_job(
        request=JobCreateRequest.model_validate(VALID_REQUEST),
        idempotency_key="request-1",
        client=AuthenticatedClient(client_id="unit-client", is_developer=True),
        repository=cast(JobRepository, stub_repository),
        settings=settings,
        demo_access=cast(DemoAccessService, StubDemoAccessService()),
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
        authenticate_client(authorization, settings, http_request(), Response())

    assert raised.value.status_code == 401
    assert raised.value.headers == {"WWW-Authenticate": "Bearer"}


def test_authentication_returns_configured_client(settings: Settings) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")

    client = authenticate_client(credentials, settings, http_request(), Response())

    assert client.client_id == "unit-client"
    assert client.is_developer is True


def test_anonymous_session_cookie_is_signed_and_stable() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        demo_mode_enabled=True,
        demo_session_secret=SecretStr("a" * 32),
    )
    first_response = Response()
    first = authenticate_client(None, settings, http_request(), first_response)
    set_cookie = first_response.headers["set-cookie"]
    cookie = set_cookie.split(";", 1)[0]

    second_response = Response()
    second = authenticate_client(None, settings, http_request(cookie), second_response)

    assert first.is_developer is False
    assert first.client_id.startswith("demo-")
    assert second.client_id == first.client_id
    assert "set-cookie" not in second_response.headers
    assert "HttpOnly" in set_cookie


def test_tampered_anonymous_cookie_gets_a_new_identity() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        demo_mode_enabled=True,
        demo_session_secret=SecretStr("b" * 32),
    )
    first_response = Response()
    first = authenticate_client(None, settings, http_request(), first_response)
    cookie = first_response.headers["set-cookie"].split(";", 1)[0]
    tampered = f"{cookie[:-1]}x"

    replacement_response = Response()
    replacement = authenticate_client(
        None, settings, http_request(tampered), replacement_response
    )

    assert replacement.client_id != first.client_id
    assert "set-cookie" in replacement_response.headers


def test_demo_mode_rejects_a_short_session_secret() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(
            database_url="postgresql+psycopg://unused",
            development_api_token=SecretStr("test-token"),
            demo_mode_enabled=True,
            demo_session_secret=SecretStr("too-short"),
        )


def test_anonymous_workload_limit_is_stricter_than_developer_limit(
    stub_repository: StubRepository,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        demo_mode_enabled=True,
        demo_session_secret=SecretStr("c" * 32),
        demo_max_monte_carlo_samples=100,
    )

    with pytest.raises(HTTPException) as raised:
        create_job(
            request=JobCreateRequest.model_validate(
                {
                    "type": "monte_carlo_pi",
                    "parameters": {"samples": 101, "seed": 42},
                }
            ),
            idempotency_key="too-large",
            client=AuthenticatedClient(client_id="demo-test", is_developer=False),
            repository=cast(JobRepository, stub_repository),
            settings=settings,
            demo_access=cast(DemoAccessService, StubDemoAccessService()),
        )

    assert raised.value.status_code == 422
    assert "limited to 100 samples" in raised.value.detail


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unsupported", "parameters": {"samples": 1000, "seed": 42}},
        {"type": "monte_carlo_pi", "parameters": {"samples": 0, "seed": 42}},
        {
            "type": "monte_carlo_pi",
            "parameters": {"samples": 1000, "seed": 42, "unexpected": True},
        },
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": ["a", "a"],
                "edges": [{"source": "a", "target": "a", "weight": 1}],
            },
        },
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": ["a", "b"],
                "edges": [{"source": "a", "target": "missing", "weight": 1}],
            },
        },
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": ["a", "b"],
                "edges": [
                    {"source": "a", "target": "b", "weight": 1},
                    {"source": "b", "target": "a", "weight": 2},
                ],
            },
        },
        {
            "type": "weighted_max_cut",
            "parameters": {"nodes": ["a", "b"], "edges": []},
        },
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": [f"node-{index}" for index in range(17)],
                "edges": [{"source": "node-0", "target": "node-1", "weight": 1}],
            },
        },
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": ["a", "b"],
                "edges": [{"source": "a", "target": "b", "weight": 0}],
            },
        },
        {"type": "weighted_max_cut", "parameters": {"samples": 1000, "seed": 42}},
        {
            "type": "bqm",
            "parameters": {
                "vartype": "BINARY",
                "variables": [f"x{index}" for index in range(17)],
                "linear": {},
                "quadratic": [],
            },
        },
        {
            "type": "bqm",
            "parameters": {
                "vartype": "BINARY",
                "variables": ["x", "y"],
                "linear": {"unknown": 1},
                "quadratic": [],
            },
        },
        {
            "type": "bqm",
            "parameters": {
                "vartype": "SPIN",
                "variables": ["x", "y"],
                "linear": {},
                "quadratic": [
                    {"source": "x", "target": "y", "bias": 1},
                    {"source": "y", "target": "x", "bias": 2},
                ],
            },
        },
        {
            "type": "cqm",
            "parameters": {
                "variables": ["x"],
                "objective": {"sense": "maximize", "linear": {"missing": 1}},
                "constraints": [
                    {"name": "limit", "linear": {"x": 1}, "sense": "<=", "rhs": 1}
                ],
            },
        },
        {
            "type": "cqm",
            "parameters": {
                "variables": ["x"],
                "objective": {"sense": "minimize", "linear": {"x": 1}},
                "constraints": [],
            },
        },
        {
            "type": "cqm",
            "parameters": {
                "variables": [f"x{index}" for index in range(17)],
                "objective": {"sense": "minimize", "linear": {}},
                "constraints": [
                    {"name": "limit", "linear": {"x0": 1}, "sense": "<=", "rhs": 1}
                ],
            },
        },
        {"type": "bqm", "parameters": {"samples": 1000, "seed": 42}},
    ],
)
def test_create_job_validates_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate(payload)


def test_create_job_accepts_bounded_weighted_graph() -> None:
    request = JobCreateRequest.model_validate(
        {
            "type": "weighted_max_cut",
            "parameters": {
                "nodes": ["a", "b", "c"],
                "edges": [
                    {"source": "a", "target": "b", "weight": 2.5},
                    {"source": "b", "target": "c", "weight": 1},
                ],
            },
        }
    )

    assert request.type.value == "weighted_max_cut"
    assert request.parameters.model_dump(mode="json")["nodes"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "bqm",
            "parameters": {
                "vartype": "BINARY",
                "variables": ["x", "y"],
                "linear": {"x": -1},
                "quadratic": [{"source": "x", "target": "y", "bias": 2}],
                "offset": 0,
            },
        },
        {
            "type": "cqm",
            "parameters": {
                "variables": ["x", "y"],
                "objective": {"sense": "maximize", "linear": {"x": 2, "y": 1}},
                "constraints": [
                    {
                        "name": "choose-one",
                        "linear": {"x": 1, "y": 1},
                        "sense": "<=",
                        "rhs": 1,
                    }
                ],
            },
        },
    ],
)
def test_create_job_accepts_generic_optimization_models(payload: dict[str, object]) -> None:
    request = JobCreateRequest.model_validate(payload)

    assert request.type.value in {"bqm", "cqm"}


def test_openapi_requires_idempotency_key() -> None:
    operation = cast(dict[str, Any], app.openapi()["paths"]["/v1/jobs"]["post"])
    header = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "Idempotency-Key"
    )

    assert header["in"] == "header"
    assert header["required"] is True


def test_conflicting_idempotency_key_returns_conflict(
    stub_repository: StubRepository, settings: Settings
) -> None:
    stub_repository.create_error = IdempotencyConflictError()

    with pytest.raises(HTTPException) as raised:
        create_job(
            request=JobCreateRequest.model_validate(VALID_REQUEST),
            idempotency_key="request-1",
            client=AuthenticatedClient(client_id="unit-client", is_developer=True),
            repository=cast(JobRepository, stub_repository),
            settings=settings,
            demo_access=cast(DemoAccessService, StubDemoAccessService()),
        )

    assert raised.value.status_code == 409
