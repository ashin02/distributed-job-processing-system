"""HTTP routes for accepting and reading jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from computegrid_api.api.auth import AuthenticatedClient, authenticate_client
from computegrid_api.api.schemas import (
    JobAcceptedResponse,
    JobAttemptResponse,
    JobCreateRequest,
    JobDetailResponse,
    JobListItem,
    JobListResponse,
    JobType,
)
from computegrid_api.db.models import Job, JobAttempt
from computegrid_api.jobs.repository import (
    IdempotencyConflictError,
    JobRepository,
    get_job_repository,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


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
) -> JobAcceptedResponse:
    """Durably accept a job and its outbox event."""
    idempotency_key = idempotency_key.strip()
    try:
        result = repository.create(
            client_id=client.client_id,
            idempotency_key=idempotency_key,
            request=request,
        )
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request",
        ) from error
    return accepted_response(result.job)


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job(
    job_id: UUID,
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    repository: Annotated[JobRepository, Depends(get_job_repository)],
) -> JobDetailResponse:
    """Return one client-visible job."""
    job = repository.get(client_id=client.client_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return detail_response(job)


@router.get("", response_model=JobListResponse)
def list_jobs(
    client: Annotated[AuthenticatedClient, Depends(authenticate_client)],
    repository: Annotated[JobRepository, Depends(get_job_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """Return a paginated newest-first list for the authenticated client."""
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
