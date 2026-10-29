"""Pydantic schemas for the jobs API."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from computegrid_api.db.models import AttemptState, JobState


class JobType(StrEnum):
    """Compute tasks accepted by the first service version."""

    MONTE_CARLO_PI = "monte_carlo_pi"


class MonteCarloPiParameters(BaseModel):
    """Validated parameters for deterministic Monte Carlo pi estimation."""

    model_config = ConfigDict(extra="forbid")

    samples: int = Field(ge=1, le=10_000_000)
    seed: int


class JobCreateRequest(BaseModel):
    """Request to create one allowlisted compute job."""

    model_config = ConfigDict(extra="forbid")

    type: JobType
    parameters: MonteCarloPiParameters


class JobAcceptedResponse(BaseModel):
    """Reference returned when a job has been durably accepted."""

    job_id: UUID
    state: JobState
    status_url: str


class JobAttemptResponse(BaseModel):
    """Persisted execution-attempt metadata."""

    attempt_id: UUID
    attempt_number: int
    state: AttemptState
    worker_id: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


class JobDetailResponse(BaseModel):
    """Current authoritative state of a job."""

    job_id: UUID
    type: JobType
    parameters: dict[str, object]
    state: JobState
    result: dict[str, object] | None
    error: dict[str, object] | None
    attempts: list[JobAttemptResponse]
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class JobListItem(BaseModel):
    """Compact job representation used in paginated listings."""

    job_id: UUID
    type: JobType
    state: JobState
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    """One offset-based page of client-visible jobs."""

    items: list[JobListItem]
    total: int
    limit: int
    offset: int
