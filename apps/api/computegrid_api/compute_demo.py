"""Bounded, durable computational feeds advanced by the existing relay."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.config import ComputeDemoSettings
from computegrid_api.db.models import DemoRun, Job, JobState
from computegrid_api.db.session import SessionFactory
from computegrid_api.jobs.repository import build_job_accepted_event, request_hash

TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


def presets(settings: ComputeDemoSettings) -> list[JobCreateRequest]:
    """Deterministic dense models within the existing solver validation bounds."""
    n = settings.compute_demo_variables
    variables = [f"x{i}" for i in range(n)]
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    payloads = [
        {"type": "monte_carlo_pi", "parameters": {
            "samples": settings.compute_demo_samples, "seed": 42}},
        {"type": "weighted_max_cut", "parameters": {
            "nodes": variables, "edges": [
                {"source": variables[i], "target": variables[j], "weight": 1 + (i * 7 + j) % 9}
                for i, j in pairs]}},
        {"type": "bqm", "parameters": {
            "vartype": "BINARY", "variables": variables,
            "linear": {v: -(i + 1) for i, v in enumerate(variables)},
            "quadratic": [{"source": variables[i], "target": variables[j],
                           "bias": (i * 3 + j) % 11 - 5} for i, j in pairs]}},
        {"type": "cqm", "parameters": {
            "variables": variables,
            "objective": {"sense": "maximize", "linear": {
                v: 1 + (i * 7) % 13 for i, v in enumerate(variables)}},
            "constraints": [{"name": f"resource-{k}", "sense": "<=", "rhs": n * 5,
                             "linear": {v: 1 + ((i + 1) * (k + 3) + i*i + k*k) % 17
                                        for i, v in enumerate(variables)}}
                            for k in range(n)]}},
    ]
    return [JobCreateRequest.model_validate(payload) for payload in payloads]


class FeedRejected(Exception):
    """A persisted admission or cooldown limit prevents a new feed."""


class RunProgress(BaseModel):
    id: UUID
    started_at: datetime
    ends_at: datetime
    remaining_seconds: float
    feed_status: str
    submitted: int
    counts: dict[JobState, int]
    execution_complete: bool
    reason: str | None


def lock_and_time(session: Session, now: datetime | None) -> datetime:
    # Shared with ordinary anonymous admission. No workers acquire this lock.
    session.execute(text("SET LOCAL statement_timeout = '5s'"))
    session.execute(text("SELECT pg_advisory_xact_lock(1129270607)"))
    if now is not None:
        return now
    current: datetime = session.execute(select(func.clock_timestamp())).scalar_one()
    return current


def start_run(factory: SessionFactory, client_id: str, key: str,
              settings: ComputeDemoSettings, *, now: datetime | None = None) -> UUID:
    with factory.begin() as session:
        current = lock_and_time(session, now)
        existing = session.scalar(select(DemoRun).where(
            DemoRun.client_id == client_id, DemoRun.request_key == key))
        if existing is not None:
            return existing.id
        latest = session.scalar(select(DemoRun).where(DemoRun.client_id == client_id)
                                .order_by(DemoRun.started_at.desc()).limit(1))
        if latest and current < latest.started_at + timedelta(
            seconds=settings.compute_demo_cooldown_seconds
        ):
            raise FeedRejected("A feed is active or the start cooldown has not elapsed")
        feeds = session.scalar(select(func.count()).select_from(DemoRun).where(
            DemoRun.ends_at > current, DemoRun.submitted < DemoRun.max_jobs)) or 0
        if feeds >= settings.compute_demo_max_feeds:
            raise FeedRejected("Global feed capacity exhausted")
        retained_runs = session.scalar(select(func.count()).select_from(DemoRun)) or 0
        if retained_runs >= settings.compute_demo_retained_capacity:
            raise FeedRejected("Retained demo run capacity exhausted")
        if not capacity_available(session, settings):
            raise FeedRejected("Global job capacity exhausted")
        run = DemoRun(
            id=uuid4(), client_id=client_id, request_key=key, started_at=current,
            ends_at=current + timedelta(seconds=20), next_at=current, submitted=0,
            max_jobs=settings.compute_demo_max_jobs,
            interval_seconds=settings.compute_demo_interval_seconds,
            presets=[p.model_dump(mode="json") for p in presets(settings)],
        )
        session.add(run)
        return run.id


def capacity_available(session: Session, settings: ComputeDemoSettings) -> bool:
    active = session.scalar(select(func.count()).select_from(Job).where(
        Job.state.not_in(TERMINAL))) or 0
    retained = session.scalar(select(func.count()).select_from(Job)) or 0
    return (active < settings.compute_demo_active_capacity
            and retained < settings.compute_demo_retained_capacity)


def advance_feeds(factory: SessionFactory, settings: ComputeDemoSettings,
                  *, now: datetime | None = None) -> int:
    """One job per due feed per tick, no catch-up bursts after downtime."""
    submitted = 0
    with factory.begin() as session:
        current = lock_and_time(session, now)
        runs = session.scalars(select(DemoRun).where(
            DemoRun.ends_at > current, DemoRun.next_at <= current,
            DemoRun.submitted < DemoRun.max_jobs).order_by(DemoRun.next_at).limit(4))
        for run in runs:
            run.next_at = current + timedelta(seconds=run.interval_seconds)
            if not capacity_available(session, settings):
                run.reason = "Global job capacity exhausted; feed throttled"
                continue
            admission_time = now or session.execute(select(func.clock_timestamp())).scalar_one()
            if admission_time >= run.ends_at:
                continue
            payload = JobCreateRequest.model_validate(run.presets[run.submitted % 4])
            job = Job(
                id=uuid5(run.id, str(run.submitted)), demo_run_id=run.id,
                client_id=run.client_id, idempotency_key=f"demo:{run.id}:{run.submitted}",
                request_hash=request_hash(payload), job_type=payload.type.value,
                parameters=payload.parameters.model_dump(mode="json"), state=JobState.ACCEPTED,
            )
            session.add_all([job, build_job_accepted_event(job)])
            run.submitted += 1
            session.flush()
            submitted += 1
    return submitted


def get_progress(factory: SessionFactory, client_id: str, run_id: UUID | None = None,
                 *, now: datetime | None = None) -> RunProgress | None:
    with factory() as session:
        # Keep the cursor and job counts in one consistent read snapshot.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        statement = select(DemoRun).where(DemoRun.client_id == client_id)
        if run_id is not None:
            statement = statement.where(DemoRun.id == run_id)
        run = session.scalar(statement.order_by(DemoRun.started_at.desc()).limit(1))
        if run is None:
            return None
        current = now or session.execute(select(func.clock_timestamp())).scalar_one()
        counts = {state: 0 for state in JobState}
        for state, count in session.execute(select(Job.state, func.count()).where(
            Job.demo_run_id == run.id).group_by(Job.state)):
            counts[state] = count
        feeding = current < run.ends_at and run.submitted < run.max_jobs
        return RunProgress(
            id=run.id, started_at=run.started_at, ends_at=run.ends_at,
            remaining_seconds=max(0, (run.ends_at - current).total_seconds()) if feeding else 0,
            feed_status="FEEDING" if feeding else "COMPLETE", submitted=run.submitted,
            counts=counts, execution_complete=not feeding and sum(
                counts[state] for state in TERMINAL) == run.submitted,
            reason=run.reason,
        )
