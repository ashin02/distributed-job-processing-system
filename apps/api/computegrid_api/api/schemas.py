"""Pydantic schemas for the jobs API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from computegrid_api.db.models import AttemptState, JobState


class JobType(StrEnum):
    """Compute tasks accepted by the first service version."""

    MONTE_CARLO_PI = "monte_carlo_pi"
    WEIGHTED_MAX_CUT = "weighted_max_cut"


class MonteCarloPiParameters(BaseModel):
    """Validated parameters for deterministic Monte Carlo pi estimation."""

    model_config = ConfigDict(extra="forbid")

    samples: int = Field(ge=1, le=10_000_000)
    seed: int


class WeightedEdge(BaseModel):
    """One positive, undirected weighted graph edge."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)


class WeightedMaxCutParameters(BaseModel):
    """A small graph that remains safe for exact local enumeration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nodes: list[str] = Field(min_length=2, max_length=16)
    edges: list[WeightedEdge] = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_graph(self) -> WeightedMaxCutParameters:
        if any(not node or len(node) > 64 for node in self.nodes):
            raise ValueError("nodes must contain non-empty labels of at most 64 characters")
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError("node labels must be unique")

        node_set = set(self.nodes)
        edge_keys: set[frozenset[str]] = set()
        for edge in self.edges:
            if edge.source == edge.target:
                raise ValueError("self-loop edges are not supported")
            if edge.source not in node_set or edge.target not in node_set:
                raise ValueError("every edge endpoint must reference a declared node")
            key = frozenset((edge.source, edge.target))
            if key in edge_keys:
                raise ValueError("duplicate undirected edges are not supported")
            edge_keys.add(key)
        return self


class JobCreateRequest(BaseModel):
    """Request to create one allowlisted compute job."""

    model_config = ConfigDict(extra="forbid")

    type: JobType
    parameters: MonteCarloPiParameters | WeightedMaxCutParameters

    @model_validator(mode="after")
    def validate_parameters_for_type(self) -> JobCreateRequest:
        expected_type = (
            MonteCarloPiParameters
            if self.type == JobType.MONTE_CARLO_PI
            else WeightedMaxCutParameters
        )
        if not isinstance(self.parameters, expected_type):
            raise ValueError(f"parameters do not match job type {self.type.value}")
        return self


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
