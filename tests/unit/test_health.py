"""Tests for the API liveness endpoint."""

from collections.abc import Iterator
from typing import cast

import httpx
import pytest
from computegrid_api.db.session import (
    SessionFactory,
    create_session_factory,
    get_session_factory,
)
from computegrid_api.main import app
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


@pytest.fixture
def anyio_backend() -> str:
    """Run AnyIO tests with the asyncio backend available in the project."""
    return "asyncio"


@pytest.mark.anyio
async def test_healthz_returns_ok() -> None:
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == httpx.codes.OK
    assert response.json() == {"status": "ok"}


@pytest.fixture
def healthy_session_factory() -> Iterator[SessionFactory]:
    """Provide a dependency-free database for the portable readiness unit test."""
    engine = create_engine("sqlite://")
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_readyz_returns_ok_when_database_responds(
    healthy_session_factory: SessionFactory,
) -> None:
    app.dependency_overrides[get_session_factory] = lambda: healthy_session_factory
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_session_factory, None)

    assert response.status_code == httpx.codes.OK
    assert response.json() == {"status": "ready", "database": "ok"}


@pytest.mark.anyio
async def test_readyz_returns_service_unavailable_when_database_fails() -> None:
    def unavailable_session() -> Session:
        raise OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

    unavailable_factory = cast(SessionFactory, unavailable_session)
    app.dependency_overrides[get_session_factory] = lambda: unavailable_factory
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_session_factory, None)

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "PostgreSQL is unavailable"}


@pytest.mark.anyio
async def test_local_frontend_cors_preflight() -> None:
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options("/v1/jobs", headers=headers)

    assert response.status_code == httpx.codes.OK
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"
