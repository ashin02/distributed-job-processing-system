"""Concurrency-safe job admission and bounded retention."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from computegrid_api.db.models import DemoRun, Job, JobState, OutboxEvent

ADMISSION_LOCK_ID = 1129270607
ACTIVE_STATES = (
    JobState.ACCEPTED,
    JobState.QUEUED,
    JobState.RUNNING,
    JobState.RETRY_WAIT,
)
TERMINAL_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class JobCapacityExceededError(Exception):
    """No safe retained slot or active-job slot is available."""


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    """Limits checked before adding one job.

    Client limits apply only to individually submitted jobs. Global limits cover
    both individual jobs and jobs belonging to Run Demo feeds.
    """

    max_active_global: int
    max_retained_global: int
    client_id: str | None = None
    max_active_per_client: int | None = None
    max_retained_per_client: int | None = None


def acquire_admission_lock(session: Session) -> None:
    """Serialize capacity checks and cleanup across all submission paths."""
    session.execute(text("SET LOCAL statement_timeout = '5s'"))
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": ADMISSION_LOCK_ID},
    )


def admit_one_job(
    session: Session,
    limits: AdmissionLimits,
    *,
    now: datetime | None = None,
) -> None:
    """Make room for one job or reject, with the admission lock already held."""
    global_active = _count(session, Job.state.in_(ACTIVE_STATES))
    if global_active >= limits.max_active_global:
        raise JobCapacityExceededError

    individual_filter = Job.demo_run_id.is_(None)
    if limits.client_id is not None:
        if limits.max_active_per_client is None or limits.max_retained_per_client is None:
            raise ValueError("client admission requires both per-client limits")
        client_active = _count(
            session,
            individual_filter,
            Job.client_id == limits.client_id,
            Job.state.in_(ACTIVE_STATES),
        )
        if client_active >= limits.max_active_per_client:
            raise JobCapacityExceededError

        client_retained = _count(
            session,
            individual_filter,
            Job.client_id == limits.client_id,
        )
        _reclaim_slots(
            session,
            required=max(0, client_retained - limits.max_retained_per_client + 1),
            now=now,
            filters=(individual_filter, Job.client_id == limits.client_id),
        )

    global_retained = _count(session)
    _reclaim_slots(
        session,
        required=max(0, global_retained - limits.max_retained_global + 1),
        now=now,
        filters=(),
    )


def _count(session: Session, *filters: ColumnElement[bool]) -> int:
    statement = select(func.count()).select_from(Job)
    if filters:
        statement = statement.where(*filters)
    return session.scalar(statement) or 0


def _reclaim_slots(
    session: Session,
    *,
    required: int,
    now: datetime | None,
    filters: tuple[ColumnElement[bool], ...],
) -> None:
    if required == 0:
        return

    active_run = exists().where(
        DemoRun.id == Job.demo_run_id,
        DemoRun.ends_at > (now if now is not None else func.clock_timestamp()),
    )
    unpublished_event = exists().where(
        OutboxEvent.job_id == Job.id,
        OutboxEvent.published_at.is_(None),
    )
    candidates = list(
        session.scalars(
            select(Job.id)
            .where(
                Job.state.in_(TERMINAL_STATES),
                ~unpublished_event,
                ~active_run,
                *filters,
            )
            .order_by(Job.created_at, Job.id)
            .limit(required)
            .with_for_update()
        )
    )
    if len(candidates) != required:
        raise JobCapacityExceededError

    # The outbox FK is intentionally RESTRICT. Remove only confirmed events
    # before their terminal jobs; attempts continue to use their CASCADE FK.
    session.execute(
        delete(OutboxEvent).where(
            OutboxEvent.job_id.in_(candidates),
            OutboxEvent.published_at.is_not(None),
        )
    )
    deleted = list(
        session.scalars(
            delete(Job).where(Job.id.in_(candidates)).returning(Job.id)
        )
    )
    if len(deleted) != required:
        raise JobCapacityExceededError
