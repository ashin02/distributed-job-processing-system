"""Authenticated, read-only operational API routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from computegrid_api.api.auth import AuthenticatedClient, authenticate_client
from computegrid_api.config import (
    Settings,
    get_broker_settings,
    get_operations_settings,
    get_settings,
)
from computegrid_api.db.session import get_session_factory
from computegrid_api.demo_access import (
    DemoAccessService,
    DemoProtectionUnavailableError,
    DemoRateLimit,
    DemoRateLimitExceededError,
    get_demo_access_service,
)
from computegrid_api.operations import (
    OperationsService,
    OperationsSummary,
    PublicOperationsSummary,
    public_operations_summary,
)


def get_operations_service() -> OperationsService:
    """Build the service from process-wide dependency settings."""
    return OperationsService(
        session_factory=get_session_factory(),
        broker_settings=get_broker_settings(),
        operations_settings=get_operations_settings(),
    )


router = APIRouter(prefix="/v1/operations", tags=["operations"])


@router.get("/summary", response_model=OperationsSummary | PublicOperationsSummary)
def operations_summary(
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    service: Annotated[OperationsService, Depends(get_operations_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    demo_access: Annotated[DemoAccessService, Depends(get_demo_access_service)],
) -> OperationsSummary | PublicOperationsSummary:
    """Return a full developer or identity-safe anonymous snapshot."""
    if not client.is_developer:
        try:
            demo_access.check(
                subject="all-anonymous-visitors",
                budget=DemoRateLimit(
                    scope="global-requests",
                    limit=settings.demo_global_request_limit,
                    window_seconds=settings.demo_rate_window_seconds,
                ),
            )
            demo_access.check(
                subject=client.client_id,
                budget=DemoRateLimit(
                    scope="operations-reads",
                    limit=settings.demo_operations_limit,
                    window_seconds=settings.demo_rate_window_seconds,
                ),
            )
        except DemoRateLimitExceededError as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Anonymous demo rate limit exceeded; retry later",
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except DemoProtectionUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Anonymous demo protection is temporarily unavailable",
            ) from error
    summary = service.summary()
    return summary if client.is_developer else public_operations_summary(summary)
