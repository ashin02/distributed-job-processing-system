"""Durable scheduling, concurrency, isolation and existing outbox integration."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from computegrid_api import compute_demo
from computegrid_api.compute_demo import FeedRejected, advance_feeds, get_progress, start_run
from computegrid_api.config import ComputeDemoSettings, Settings, get_settings
from computegrid_api.db.models import DemoRun, Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, get_session_factory
from computegrid_api.main import app
from computegrid_api.outbox_relay import relay_once
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 30, tzinfo=UTC)


@pytest.fixture
def policy() -> ComputeDemoSettings:
    return ComputeDemoSettings(  # type: ignore[call-arg]
        _env_file=None, compute_demo_interval_seconds=0.5, compute_demo_max_jobs=40,
        compute_demo_max_feeds=1, compute_demo_active_capacity=40,
        compute_demo_retained_capacity=5000, compute_demo_cooldown_seconds=120,
        compute_demo_samples=100, compute_demo_variables=4,
    )


def test_duplicate_start_and_controller_restart(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    run_id = start_run(session_factory, "a", "request", policy, now=NOW)
    assert start_run(session_factory, "a", "request", policy, now=NOW) == run_id
    assert advance_feeds(session_factory, policy, now=NOW) == 1
    assert advance_feeds(session_factory, policy, now=NOW) == 0
    # A new controller invocation needs no process-local state.
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=6)) == 1
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=20)) == 0
    progress = get_progress(session_factory, "a", run_id, now=NOW + timedelta(seconds=20))
    assert progress is not None
    assert progress.feed_status == "COMPLETE"
    assert progress.submitted == 2
    assert not progress.execution_complete
    assert get_progress(session_factory, "b", run_id, now=NOW) is None
    with session_factory() as session:
        jobs = list(session.scalars(select(Job)))
        assert {job.client_id for job in jobs} == {"a"}
        assert {job.demo_run_id for job in jobs} == {run_id}
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2
    with session_factory.begin() as session:
        for job in session.scalars(select(Job)):
            job.state = JobState.FAILED
    finished = get_progress(session_factory, "a", now=NOW + timedelta(seconds=30))
    assert finished is not None and finished.execution_complete


def test_fractional_interval_submits_once_per_due_tick_without_catch_up(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    start_run(session_factory, "a", "fractional", policy, now=NOW)
    assert advance_feeds(session_factory, policy, now=NOW) == 1
    assert advance_feeds(
        session_factory, policy, now=NOW + timedelta(seconds=0.49)
    ) == 0
    assert advance_feeds(
        session_factory, policy, now=NOW + timedelta(seconds=0.5)
    ) == 1
    # A late poll advances by one job, never by the number of missed intervals.
    assert advance_feeds(
        session_factory, policy, now=NOW + timedelta(seconds=10)
    ) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 3


def test_demo_run_database_bounds_match_settings(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    policy.compute_demo_interval_seconds = 0.1
    policy.compute_demo_max_jobs = 40
    run_id = start_run(session_factory, "bounds", "accepted", policy, now=NOW)
    with session_factory() as session:
        run = session.get(DemoRun, run_id)
        assert run is not None
        assert run.interval_seconds == 0.1
        assert run.max_jobs == 40
        assert run.ends_at - run.started_at == timedelta(seconds=20)

    with pytest.raises(IntegrityError), session_factory.begin() as session:
        run = session.get(DemoRun, run_id)
        assert run is not None
        run.interval_seconds = 0.09

    with pytest.raises(IntegrityError), session_factory.begin() as session:
        run = session.get(DemoRun, run_id)
        assert run is not None
        run.max_jobs = 41


def test_concurrent_starts_and_ticks(session_factory: SessionFactory,
                                    policy: ComputeDemoSettings) -> None:
    def start(_: int) -> object:
        return start_run(session_factory, "a", "same", policy, now=NOW)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert len(set(executor.map(start, range(4)))) == 1
        counts = list(executor.map(
            lambda _: advance_feeds(session_factory, policy, now=NOW), range(4)))
    assert sum(counts) == 1
    with pytest.raises(FeedRejected):
        start_run(session_factory, "a", "new-key", policy, now=NOW)
    with pytest.raises(FeedRejected):
        start_run(session_factory, "b", "new-key", policy, now=NOW)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DemoRun)) == 1


def test_capacity_throttles_without_extending_window(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    policy.compute_demo_active_capacity = 1
    run_id = start_run(session_factory, "a", "run", policy, now=NOW)
    assert advance_feeds(session_factory, policy, now=NOW) == 1
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=2)) == 0
    progress = get_progress(session_factory, "a", run_id, now=NOW + timedelta(seconds=2))
    assert progress and progress.reason and progress.remaining_seconds == 18
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=21)) == 0


def test_demo_uses_existing_confirmed_outbox_pipeline(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    start_run(session_factory, "a", "run", policy, now=NOW)
    advance_feeds(session_factory, policy, now=NOW)
    published: list[OutboxEvent] = []

    class Publisher:
        def publish(self, event: OutboxEvent) -> None:
            published.append(event)

    assert relay_once(session_factory, Publisher(), batch_size=50) == 1
    assert published[0].event_type == "job.accepted"
    with session_factory() as session:
        assert session.scalar(select(Job.state)) == JobState.QUEUED


def test_maximum_jobs_and_retained_cap(session_factory: SessionFactory,
                                      policy: ComputeDemoSettings) -> None:
    policy.compute_demo_max_jobs = 1
    start_run(session_factory, "a", "run", policy, now=NOW)
    assert advance_feeds(session_factory, policy, now=NOW) == 1
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=2)) == 0
    policy.compute_demo_retained_capacity = 1
    with pytest.raises(FeedRejected):
        start_run(session_factory, "a", "later", policy, now=NOW + timedelta(seconds=121))


def test_http_anonymous_run_ownership_and_authentication(session_factory: SessionFactory) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql+psycopg://unused",
        development_api_token=SecretStr("test-token"), demo_mode_enabled=True,
        demo_session_secret=SecretStr("s" * 64),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        with TestClient(app) as owner, TestClient(app) as stranger:
            started = owner.post("/v1/demo-runs", headers={
                "Origin": "http://localhost:5173", "Idempotency-Key": "start"})
            assert started.status_code == 202
            run_id = started.json()["id"]
            assert owner.get("/v1/demo-runs/latest").json()["id"] == run_id
            assert stranger.get(f"/v1/demo-runs/{run_id}").status_code == 404
            assert stranger.get("/v1/demo-runs/latest").json() is None
            assert owner.get("/v1/demo-runs/latest", headers={
                "Authorization": "Bearer invalid"}).status_code == 401
            settings.demo_mode_enabled = False
            assert owner.get("/v1/demo-runs/latest").status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_session_factory, None)


def test_job_outbox_and_cursor_rollback_together(
    session_factory: SessionFactory, policy: ComputeDemoSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = start_run(session_factory, "a", "rollback", policy, now=NOW)

    def bad_event(job: Job) -> OutboxEvent:
        return OutboxEvent(
            job_id=job.id,
            event_type="job.accepted",
            payload={},
            publish_attempts=-1,
        )

    monkeypatch.setattr(compute_demo, "build_job_accepted_event", bad_event)
    with pytest.raises(IntegrityError):
        advance_feeds(session_factory, policy, now=NOW)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert session.scalar(select(DemoRun.submitted).where(DemoRun.id == run_id)) == 0


def test_restart_after_deadline_does_not_submit(
    session_factory: SessionFactory, policy: ComputeDemoSettings,
) -> None:
    run_id = start_run(session_factory, "a", "late", policy, now=NOW)
    assert advance_feeds(session_factory, policy, now=NOW + timedelta(seconds=25)) == 0
    progress = get_progress(session_factory, "a", run_id, now=NOW + timedelta(seconds=25))
    assert progress and progress.submitted == 0 and progress.execution_complete
