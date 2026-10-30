"""Pydantic schemas for the jobs API."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from computegrid_api.db.models import AttemptState, JobState


class JobType(StrEnum):
    """Compute tasks accepted by the first service version."""

    MONTE_CARLO_PI = "monte_carlo_pi"
    WEIGHTED_MAX_CUT = "weighted_max_cut"
    BQM = "bqm"
    CQM = "cqm"


class BQMVariableType(StrEnum):
    """Domains supported by the exact Binary Quadratic Model solver."""

    BINARY = "BINARY"
    SPIN = "SPIN"


class ObjectiveSense(StrEnum):
    """Direction for a restricted CQM objective."""

    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class ConstraintSense(StrEnum):
    """Relations supported by restricted linear constraints."""

    LESS_THAN_EQUAL = "<="
    GREATER_THAN_EQUAL = ">="
    EQUAL = "=="


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


class QuadraticBias(BaseModel):
    """One symmetric interaction in a Binary Quadratic Model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    bias: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)


class BQMParameters(BaseModel):
    """A bounded BINARY QUBO or SPIN Ising model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vartype: BQMVariableType
    variables: list[str] = Field(min_length=1, max_length=16)
    linear: dict[str, float] = Field(default_factory=dict, max_length=16)
    quadratic: list[QuadraticBias] = Field(default_factory=list, max_length=120)
    offset: float = Field(default=0, ge=-1_000_000, le=1_000_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_model(self) -> BQMParameters:
        _validate_variable_labels(self.variables)
        variable_set = set(self.variables)
        if not set(self.linear).issubset(variable_set):
            raise ValueError("linear biases must reference declared variables")
        if any(not key or len(key) > 64 for key in self.linear):
            raise ValueError("linear bias keys must be non-empty labels of at most 64 characters")
        _validate_finite_biases(self.linear.values(), "linear biases")

        interactions: set[frozenset[str]] = set()
        for term in self.quadratic:
            if term.source == term.target:
                raise ValueError("quadratic self-interactions must be expressed as linear biases")
            if term.source not in variable_set or term.target not in variable_set:
                raise ValueError("quadratic biases must reference declared variables")
            key = frozenset((term.source, term.target))
            if key in interactions:
                raise ValueError("duplicate symmetric quadratic interactions are not supported")
            interactions.add(key)
        return self


class CQMObjective(BaseModel):
    """Linear objective for the restricted CQM subset."""

    model_config = ConfigDict(extra="forbid")

    sense: ObjectiveSense
    linear: dict[str, float] = Field(default_factory=dict, max_length=16)
    offset: float = Field(default=0, ge=-1_000_000, le=1_000_000, allow_inf_nan=False)


class CQMConstraint(BaseModel):
    """One named linear constraint over binary variables."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64)
    linear: dict[str, float] = Field(min_length=1, max_length=16)
    sense: ConstraintSense
    rhs: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)


class CQMParameters(BaseModel):
    """A deliberately restricted binary, linear constrained model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    variables: list[str] = Field(min_length=1, max_length=16)
    objective: CQMObjective
    constraints: list[CQMConstraint] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_model(self) -> CQMParameters:
        _validate_variable_labels(self.variables)
        variable_set = set(self.variables)
        _validate_linear_expression(self.objective.linear, variable_set, "objective")

        names: set[str] = set()
        for constraint in self.constraints:
            if constraint.name in names:
                raise ValueError("constraint names must be unique")
            names.add(constraint.name)
            _validate_linear_expression(
                constraint.linear,
                variable_set,
                f"constraint {constraint.name}",
            )
        return self


def _validate_variable_labels(variables: list[str]) -> None:
    if any(not variable or len(variable) > 64 for variable in variables):
        raise ValueError("variables must have non-empty labels of at most 64 characters")
    if len(set(variables)) != len(variables):
        raise ValueError("variable labels must be unique")


def _validate_finite_biases(values: Iterable[float], label: str) -> None:
    for value in values:
        if not -1_000_000 <= value <= 1_000_000:
            raise ValueError(f"{label} must be finite and between -1000000 and 1000000")


def _validate_linear_expression(
    expression: dict[str, float], variable_set: set[str], label: str
) -> None:
    if not set(expression).issubset(variable_set):
        raise ValueError(f"{label} coefficients must reference declared variables")
    if any(not key or len(key) > 64 for key in expression):
        raise ValueError(f"{label} coefficient keys must be valid variable labels")
    _validate_finite_biases(expression.values(), f"{label} coefficients")


class JobCreateRequest(BaseModel):
    """Request to create one allowlisted compute job."""

    model_config = ConfigDict(extra="forbid")

    type: JobType
    parameters: MonteCarloPiParameters | WeightedMaxCutParameters | BQMParameters | CQMParameters

    @model_validator(mode="after")
    def validate_parameters_for_type(self) -> JobCreateRequest:
        expected_types = {
            JobType.MONTE_CARLO_PI: MonteCarloPiParameters,
            JobType.WEIGHTED_MAX_CUT: WeightedMaxCutParameters,
            JobType.BQM: BQMParameters,
            JobType.CQM: CQMParameters,
        }
        expected_type = expected_types[self.type]
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
