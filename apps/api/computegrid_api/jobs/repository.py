"""Transactional persistence operations for jobs."""

import logging
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.db.models import Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, get_session_factory, transaction

logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """The same client key was used for a different request payload."""


class DemoCapacityExceededError(Exception):
    """Anonymous demo capacity is currently exhausted."""


@dataclass(frozen=True, slots=True)
class JobCreationLimits:
    """Admission limits applied atomically to anonymous job creation."""

    max_active_per_client: int
    max_active_global: int
    max_retained_per_client: int
    max_retained_global: int


@dataclass(frozen=True, slots=True)
class CreatedJob:
    """Result of an idempotent create operation."""

    job: Job
    created: bool


@dataclass(frozen=True, slots=True)
class JobPage:
    """A page of jobs and the total matching row count."""

    items: list[Job]
    total: int


def request_hash(request: JobCreateRequest) -> str:
    """Return a stable hash of a validated job request."""
    canonical_payload = dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(canonical_payload).hexdigest()


def build_job_accepted_event(job: Job) -> OutboxEvent:
    """Build the event committed in the same transaction as a new job."""
    return OutboxEvent(
        id=uuid4(),
        job_id=job.id,
        event_type="job.accepted",
        payload={"job_id": str(job.id), "job_type": job.job_type, "attempt_number": 1},
    )


class JobRepository:
    """PostgreSQL-backed job operations with client isolation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        request: JobCreateRequest,
        limits: JobCreationLimits | None = None,
    ) -> CreatedJob:
        """Create a job and outbox event, or return an identical prior request."""
        payload_hash = request_hash(request)
        existing = self._find_by_idempotency_key(client_id, idempotency_key)
        if existing is not None:
            return self._resolve_existing(existing, payload_hash)

        job = Job(
            id=uuid4(),
            client_id=client_id,
            idempotency_key=idempotency_key,
            request_hash=payload_hash,
            job_type=request.type.value,
            parameters=request.parameters.model_dump(mode="json"),
        )
        event = build_job_accepted_event(job)

        try:
            with transaction(self._session_factory) as session:
                if limits is not None:
                    # Serialize anonymous admission across API processes without
                    # holding a lock during job execution.
                    session.execute(text("SELECT pg_advisory_xact_lock(1129270607)"))
                    existing_after_lock = session.scalar(
                        select(Job).where(
                            Job.client_id == client_id,
                            Job.idempotency_key == idempotency_key,
                        )
                    )
                    if existing_after_lock is not None:
                        return self._resolve_existing(existing_after_lock, payload_hash)
                    active_states = (
                        JobState.ACCEPTED,
                        JobState.QUEUED,
                        JobState.RUNNING,
                        JobState.RETRY_WAIT,
                    )
                    global_active = session.scalar(
                        select(func.count()).select_from(Job).where(Job.state.in_(active_states))
                    )
                    client_active = session.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(Job.client_id == client_id, Job.state.in_(active_states))
                    )
                    global_retained = session.scalar(select(func.count()).select_from(Job))
                    client_retained = session.scalar(
                        select(func.count()).select_from(Job).where(Job.client_id == client_id)
                    )
                    if (global_active or 0) >= limits.max_active_global or (
                        client_active or 0
                    ) >= limits.max_active_per_client:
                        raise DemoCapacityExceededError
                    if (global_retained or 0) >= limits.max_retained_global or (
                        client_retained or 0
                    ) >= limits.max_retained_per_client:
                        raise DemoCapacityExceededError
                session.add_all([job, event])
                session.flush()
        except IntegrityError:
            concurrent_job = self._find_by_idempotency_key(client_id, idempotency_key)
            if concurrent_job is None:
                raise
            return self._resolve_existing(concurrent_job, payload_hash)

        logger.info(
            "Job and acceptance event committed",
            extra={"event": "job.submitted", "job_id": str(job.id)},
        )
        return CreatedJob(job=job, created=True)

    def get(self, *, client_id: str, job_id: UUID) -> Job | None:
        """Return a client-visible job with attempts loaded."""
        statement = (
            select(Job)
            .options(selectinload(Job.attempts))
            .where(Job.id == job_id, Job.client_id == client_id)
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def list(self, *, client_id: str, limit: int, offset: int) -> JobPage:
        """Return a stable newest-first page for one client."""
        filters = Job.client_id == client_id
        count_statement = select(func.count()).select_from(Job).where(filters)
        page_statement = (
            select(Job)
            .where(filters)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .offset(offset)
            .limit(limit)
        )
        with self._session_factory() as session:
            total = session.scalar(count_statement)
            items = list(session.scalars(page_statement))
        return JobPage(items=items, total=total or 0)

    def _find_by_idempotency_key(self, client_id: str, idempotency_key: str) -> Job | None:
        statement = select(Job).where(
            Job.client_id == client_id,
            Job.idempotency_key == idempotency_key,
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    @staticmethod
    def _resolve_existing(existing: Job, payload_hash: str) -> CreatedJob:
        if existing.request_hash != payload_hash:
            raise IdempotencyConflictError
        return CreatedJob(job=existing, created=False)


def get_job_repository() -> JobRepository:
    """Return the process database-backed repository."""
    return JobRepository(get_session_factory())
