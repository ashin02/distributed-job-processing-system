"""Session-scoped computational feed admission and progress."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from computegrid_api.api.auth import AuthenticatedClient, authenticate_client
from computegrid_api.compute_demo import FeedRejected, RunProgress, get_progress, start_run
from computegrid_api.config import get_compute_demo_settings, get_cors_settings
from computegrid_api.db.session import SessionFactory, get_session_factory
from computegrid_api.demo_access import (
    DemoAccessService,
    DemoProtectionUnavailableError,
    DemoRateLimit,
    DemoRateLimitExceededError,
)


def access(
    request: Request,
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> AuthenticatedClient:
    origin = request.headers.get("origin")
    if request.method == "POST" and (
        (origin is not None and origin not in get_cors_settings().cors_origins)
        or (not client.is_developer and origin is None)
    ):
        raise HTTPException(403, "A configured browser Origin is required")
    limiter = DemoAccessService(factory)
    try:
        limiter.check(subject="all", budget=DemoRateLimit("compute-demo-global", 240, 60))
        limiter.check(subject=client.client_id, budget=DemoRateLimit(
            "compute-demo-start" if request.method == "POST" else "compute-demo-read",
            5 if request.method == "POST" else 60, 60))
    except DemoRateLimitExceededError as error:
        raise HTTPException(429, "Demo request limit exceeded", headers={
            "Retry-After": str(error.retry_after)}) from error
    except DemoProtectionUnavailableError as error:
        raise HTTPException(503, "Demo protection unavailable") from error
    return client


router = APIRouter(prefix="/v1/demo-runs", tags=["demo-runs"])
Client = Annotated[AuthenticatedClient, Depends(access)]
Factory = Annotated[SessionFactory, Depends(get_session_factory)]


class StartRequest(BaseModel):
    """No caller-selected workload or scheduling parameters are accepted."""

    model_config = ConfigDict(extra="forbid")


@router.post("", response_model=RunProgress, status_code=202)
def create_run(
    client: Client, factory: Factory,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200, pattern=r".*\S.*")],
    body: StartRequest | None = None,
) -> RunProgress:
    try:
        run_id = start_run(factory, client.client_id, idempotency_key,
                           get_compute_demo_settings())
        result = get_progress(factory, client.client_id, run_id)
    except FeedRejected as error:
        raise HTTPException(429, str(error), headers={
            "Retry-After": str(get_compute_demo_settings().compute_demo_cooldown_seconds)
        }) from error
    except SQLAlchemyError as error:
        raise HTTPException(503, "Demo storage unavailable") from error
    assert result is not None
    return result


@router.get("/latest", response_model=RunProgress | None)
def latest_run(client: Client, factory: Factory) -> RunProgress | None:
    try:
        return get_progress(factory, client.client_id)
    except SQLAlchemyError as error:
        raise HTTPException(503, "Demo storage unavailable") from error


@router.get("/{run_id}", response_model=RunProgress)
def read_run(run_id: UUID, client: Client, factory: Factory) -> RunProgress:
    try:
        result = get_progress(factory, client.client_id, run_id)
    except SQLAlchemyError as error:
        raise HTTPException(503, "Demo storage unavailable") from error
    if result is None:
        raise HTTPException(404, "Demo run not found")
    return result
