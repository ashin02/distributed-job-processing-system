"""Focused tests for authenticated operational snapshots and dependency degradation."""

from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import URLError
from urllib.request import Request
from uuid import UUID

import httpx
import pytest
from computegrid_api import operations as operations_module
from computegrid_api.api.operations import get_operations_service
from computegrid_api.config import BrokerSettings, Settings, get_settings
from computegrid_api.db.models import JobState
from computegrid_api.db.session import SessionFactory
from computegrid_api.demo_access import DemoAccessService, get_demo_access_service
from computegrid_api.main import app
from computegrid_api.operations import (
    DependencyStatus,
    OperationsService,
    OperationsSummary,
    PostgreSQLSummary,
    RabbitMQSummary,
    WorkerActivity,
    WorkersSummary,
    WorkerSummary,
    celery_summary,
    postgresql_summary,
    public_operations_summary,
    rabbitmq_summary,
)
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


class FakeHTTPResponse:
    """Minimal context-managed HTTP response for urllib tests."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class StubOperationsService:
    """Route dependency returning one deterministic snapshot."""

    def summary(self) -> OperationsSummary:
        return OperationsSummary(
            generated_at=datetime(2026, 10, 30, tzinfo=UTC),
            rabbitmq=RabbitMQSummary(
                status=DependencyStatus.AVAILABLE,
                queue_name="computegrid.jobs",
                messages_ready=2,
                messages_unacknowledged=1,
                consumer_count=4,
            ),
            workers=WorkersSummary(
                status=DependencyStatus.AVAILABLE,
                items=[
                    WorkerSummary(
                        worker_id="celery@worker-a",
                        availability=DependencyStatus.AVAILABLE,
                        activity=WorkerActivity.IDLE,
                    )
                ],
            ),
            postgresql=PostgreSQLSummary(
                status=DependencyStatus.AVAILABLE,
                job_counts={state: 0 for state in JobState},
            ),
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def broker_settings() -> BrokerSettings:
    return BrokerSettings(
        broker_url="amqp://computegrid:localpass@rabbitmq:5672//",
        job_queue="computegrid.jobs",
    )


def test_rabbitmq_summary_returns_real_queue_counters(
    monkeypatch: pytest.MonkeyPatch,
    broker_settings: BrokerSettings,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> FakeHTTPResponse:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeHTTPResponse(
            b'{"messages_ready":3,"messages_unacknowledged":2,"consumers":4}'
        )

    monkeypatch.setattr(operations_module, "urlopen", fake_urlopen)

    summary = rabbitmq_summary(
        broker_settings,
        "http://rabbitmq:15672",
        timeout=1.5,
    )

    assert summary.status == DependencyStatus.AVAILABLE
    assert summary.messages_ready == 3
    assert summary.messages_unacknowledged == 2
    assert summary.consumer_count == 4
    assert captured["url"] == (
        "http://rabbitmq:15672/api/queues/%2F/computegrid.jobs"
    )
    assert str(captured["authorization"]).startswith("Basic ")
    assert captured["timeout"] == 1.5


def test_rabbitmq_summary_reports_unavailable_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    broker_settings: BrokerSettings,
) -> None:
    def unavailable(*_: object, **__: object) -> FakeHTTPResponse:
        raise URLError("broker unavailable")

    monkeypatch.setattr(operations_module, "urlopen", unavailable)

    summary = rabbitmq_summary(broker_settings, "http://rabbitmq:15672", timeout=1)

    assert summary.status == DependencyStatus.UNAVAILABLE
    assert summary.messages_ready is None
    assert summary.messages_unacknowledged is None
    assert summary.consumer_count is None


def test_rabbitmq_summary_reports_unknown_for_incomplete_counters(
    monkeypatch: pytest.MonkeyPatch,
    broker_settings: BrokerSettings,
) -> None:
    monkeypatch.setattr(
        operations_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeHTTPResponse(b'{"messages_ready":3}'),
    )

    summary = rabbitmq_summary(broker_settings, "http://rabbitmq:15672", timeout=1)

    assert summary.status == DependencyStatus.UNKNOWN
    assert summary.messages_ready is None


def test_celery_summary_maps_idle_busy_and_current_job(
    monkeypatch: pytest.MonkeyPatch,
    broker_settings: BrokerSettings,
) -> None:
    job_id = UUID("a2cc9b84-8fe2-43b5-a596-3c166a344437")
    active = {
        "celery@worker-a": [],
        "celery@worker-b": [
            {
                "name": "computegrid.execute_job",
                "args": [str(job_id), 1],
            }
        ],
    }
    monkeypatch.setattr(operations_module, "_inspect_active", lambda *_: active)

    summary = celery_summary(broker_settings, timeout=1)

    assert summary.status == DependencyStatus.AVAILABLE
    assert [(worker.worker_id, worker.activity) for worker in summary.items] == [
        ("celery@worker-a", WorkerActivity.IDLE),
        ("celery@worker-b", WorkerActivity.BUSY),
    ]
    assert summary.items[1].current_job_id == job_id


def test_celery_summary_reports_unavailable_when_no_worker_answers(
    monkeypatch: pytest.MonkeyPatch,
    broker_settings: BrokerSettings,
) -> None:
    monkeypatch.setattr(operations_module, "_inspect_active", lambda *_: None)

    summary = celery_summary(broker_settings, timeout=1)

    assert summary.status == DependencyStatus.UNAVAILABLE
    assert summary.items == []


def test_postgresql_summary_reports_unavailable_without_raising() -> None:
    def unavailable_session() -> Session:
        raise OperationalError("SELECT", {}, RuntimeError("database unavailable"))

    summary = postgresql_summary(
        cast(SessionFactory, unavailable_session),
        timeout_seconds=1,
    )

    assert summary.status == DependencyStatus.UNAVAILABLE
    assert summary.job_counts is None


def test_public_operations_summary_redacts_worker_and_queue_identity() -> None:
    summary = StubOperationsService().summary()

    public = public_operations_summary(summary)
    body = public.model_dump(mode="json")

    assert body["rabbitmq"]["messages_ready"] == 2
    assert "queue_name" not in body["rabbitmq"]
    assert body["workers"] == {
        "status": "AVAILABLE",
        "available": 1,
        "idle": 1,
        "busy": 0,
        "message": None,
    }
    assert "items" not in body["workers"]


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_operations_summary_requires_authentication(token: str | None) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        development_client_id="unit-client",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_operations_service] = lambda: cast(
        OperationsService, StubOperationsService()
    )
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/operations/summary", headers=headers)
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_operations_service, None)

    assert response.status_code == httpx.codes.UNAUTHORIZED


@pytest.mark.anyio
async def test_operations_summary_returns_authenticated_snapshot() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        development_client_id="unit-client",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_operations_service] = lambda: cast(
        OperationsService, StubOperationsService()
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/operations/summary",
                headers={"Authorization": "Bearer test-token"},
            )
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_operations_service, None)

    assert response.status_code == httpx.codes.OK
    body: dict[str, Any] = response.json()
    assert body["rabbitmq"]["messages_ready"] == 2
    assert body["workers"]["items"][0]["activity"] == "IDLE"
    assert body["postgresql"]["job_counts"]["ACCEPTED"] == 0


@pytest.mark.anyio
async def test_anonymous_operations_summary_exposes_only_aggregate_metrics() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"),
        demo_mode_enabled=True,
        demo_session_secret=SecretStr("d" * 32),
    )

    class NoopDemoAccess:
        def check(self, **_: object) -> None:
            return None

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_operations_service] = lambda: cast(
        OperationsService, StubOperationsService()
    )
    app.dependency_overrides[get_demo_access_service] = lambda: cast(
        DemoAccessService, NoopDemoAccess()
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/operations/summary")
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_operations_service, None)
        app.dependency_overrides.pop(get_demo_access_service, None)

    assert response.status_code == httpx.codes.OK
    body: dict[str, Any] = response.json()
    assert body["rabbitmq"]["consumer_count"] == 4
    assert "queue_name" not in body["rabbitmq"]
    assert body["workers"]["available"] == 1
    assert "items" not in body["workers"]
