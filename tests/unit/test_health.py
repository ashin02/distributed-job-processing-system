"""Tests for the API liveness endpoint."""

import httpx
import pytest
from computegrid_api.main import app


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
