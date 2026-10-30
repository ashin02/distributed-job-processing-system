"""HTTP routes for accepting and reading jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from computegrid_api.api.auth import AuthenticatedClient, authenticate_client
from computegrid_api.api.schemas import (
    BQMParameters,
    CQMParameters,
    JobAcceptedResponse,
    JobAttemptResponse,
    JobCreateRequest,
    JobDetailResponse,
    JobListItem,
    JobListResponse,
    JobType,
    MonteCarloPiParameters,
    WeightedMaxCutParameters,
)
from computegrid_api.config import Settings, get_settings
from computegrid_api.db.models import Job, JobAttempt
from computegrid_api.demo_access import (
    DemoAccessService,
    DemoProtectionUnavailableError,
    DemoRateLimit,
    DemoRateLimitExceededError,
    get_demo_access_service,
)
from computegrid_api.jobs.repository import (
    DemoCapacityExceededError,
    IdempotencyConflictError,
    JobCreationLimits,
    JobRepository,
    get_job_repository,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _enforce_demo_rate_limit(
    *,
    client: AuthenticatedClient,
    settings: Settings,
    service: DemoAccessService,
    scope: str,
    limit: int,
) -> None:
    if client.is_developer:
        return
    try:
        service.check(
            subject="all-anonymous-visitors",
            budget=DemoRateLimit(
                scope="global-requests",
                limit=settings.demo_global_request_limit,
                window_seconds=settings.demo_rate_window_seconds,
            ),
        )
        service.check(
            subject=client.client_id,
            budget=DemoRateLimit(
                scope=scope,
                limit=limit,
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


def _validate_demo_workload(request: JobCreateRequest, settings: Settings) -> None:
    parameters = request.parameters
    if isinstance(parameters, MonteCarloPiParameters):
        if parameters.samples > settings.demo_max_monte_carlo_samples:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Anonymous demo Monte Carlo jobs are limited to "
                    f"{settings.demo_max_monte_carlo_samples} samples"
                ),
            )
        return
    variables = (
        len(parameters.nodes)
        if isinstance(parameters, WeightedMaxCutParameters)
        else len(parameters.variables)
        if isinstance(parameters, BQMParameters | CQMParameters)
        else 0
    )
    if variables > settings.demo_max_model_variables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Anonymous demo optimization jobs are limited to "
                f"{settings.demo_max_model_variables} variables"
            ),
        )


def accepted_response(job: Job) -> JobAcceptedResponse:
    """Build the response shared by new and idempotently repeated submissions."""
    return JobAcceptedResponse(
        job_id=job.id,
        state=job.state,
        status_url=f"/v1/jobs/{job.id}",
    )


def attempt_response(attempt: JobAttempt) -> JobAttemptResponse:
    """Convert a persisted attempt to its API representation."""
    return JobAttemptResponse(
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        state=attempt.state,
        worker_id=attempt.worker_id,
        lease_expires_at=attempt.lease_expires_at,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        error=attempt.error,
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )


def detail_response(job: Job) -> JobDetailResponse:
    """Convert a persisted job to its detailed API representation."""
    return JobDetailResponse(
        job_id=job.id,
        type=JobType(job.job_type),
        parameters=job.parameters,
        state=job.state,
        result=job.result,
        error=job.error,
        attempts=[attempt_response(attempt) for attempt in job.attempts],
        created_at=job.created_at,
        updated_at=job.updated_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@router.post("", response_model=JobAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    request: JobCreateRequest,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r".*\S.*",
        ),
    ],
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    repository: Annotated[JobRepository, Depends(get_job_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    demo_access: Annotated[DemoAccessService, Depends(get_demo_access_service)],
) -> JobAcceptedResponse:
    """Durably accept a job and its outbox event."""
    idempotency_key = idempotency_key.strip()
    _enforce_demo_rate_limit(
        client=client,
        settings=settings,
        service=demo_access,
        scope="job-submissions",
        limit=settings.demo_submission_limit,
    )
    if not client.is_developer:
        _validate_demo_workload(request, settings)
    try:
        result = repository.create(
            client_id=client.client_id,
            idempotency_key=idempotency_key,
            request=request,
            limits=None
            if client.is_developer
            else JobCreationLimits(
                max_active_per_client=settings.demo_max_active_jobs_per_client,
                max_active_global=settings.demo_max_active_jobs_global,
                max_retained_per_client=settings.demo_max_retained_jobs_per_client,
                max_retained_global=settings.demo_max_retained_jobs_global,
            ),
        )
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request",
        ) from error
    except DemoCapacityExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Anonymous demo job capacity is currently exhausted; retry later",
            headers={"Retry-After": str(settings.demo_rate_window_seconds)},
        ) from error
    return accepted_response(result.job)


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job(
    job_id: UUID,
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    repository: Annotated[JobRepository, Depends(get_job_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    demo_access: Annotated[DemoAccessService, Depends(get_demo_access_service)],
) -> JobDetailResponse:
    """Return one client-visible job."""
    _enforce_demo_rate_limit(
        client=client,
        settings=settings,
        service=demo_access,
        scope="job-reads",
        limit=settings.demo_read_limit,
    )
    job = repository.get(client_id=client.client_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return detail_response(job)


@router.get("", response_model=JobListResponse)
def list_jobs(
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    repository: Annotated[JobRepository, Depends(get_job_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    demo_access: Annotated[DemoAccessService, Depends(get_demo_access_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """Return a paginated newest-first list for the authenticated client."""
    _enforce_demo_rate_limit(
        client=client,
        settings=settings,
        service=demo_access,
        scope="job-reads",
        limit=settings.demo_read_limit,
    )
    page = repository.list(client_id=client.client_id, limit=limit, offset=offset)
    return JobListResponse(
        items=[
            JobListItem(
                job_id=job.id,
                type=JobType(job.job_type),
                state=job.state,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in page.items
        ],
        total=page.total,
        limit=limit,
        offset=offset,
    )
