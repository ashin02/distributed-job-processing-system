"""End-to-end submission, relay, RabbitMQ, and Celery worker test."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from uuid import UUID

import pytest
from celery.contrib.testing.worker import start_worker
from computegrid_api.api.schemas import JobCreateRequest
from computegrid_api.celery_app import celery_app
from computegrid_api.config import get_database_settings
from computegrid_api.db.models import JobState, OutboxEvent
from computegrid_api.db.session import (
    SessionFactory,
    get_engine,
    get_session_factory,
)
from computegrid_api.jobs.repository import JobRepository
from computegrid_api.outbox_relay import CeleryEventPublisher, relay_once
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select

pytestmark = pytest.mark.e2e


class EndToEndSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPUTEGRID_", extra="ignore")

    test_broker_url: str = Field(min_length=1)


def wait_for_terminal(repository: JobRepository, job_id: UUID, timeout: float = 15) -> JobState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = repository.get(client_id="e2e-client", job_id=job_id)
        assert job is not None
        if job.state in {JobState.SUCCEEDED, JobState.FAILED}:
            return job.state
        time.sleep(0.1)
    pytest.fail("job did not reach a terminal state before timeout")


def test_submission_relay_worker_and_duplicate_delivery(
    session_factory: SessionFactory,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_url = EndToEndSettings().test_broker_url  # type: ignore[call-arg]
    broker = urlparse(broker_url)
    if broker.hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("The end-to-end broker must be local")

    monkeypatch.setenv("COMPUTEGRID_DATABASE_URL", test_database_url)
    get_database_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    celery_app.conf.broker_url = broker_url
    celery_app.control.purge()

    repository = JobRepository(session_factory)
    created = repository.create(
        client_id="e2e-client",
        idempotency_key="e2e-execution",
        request=JobCreateRequest.model_validate(
            {
                "type": "monte_carlo_pi",
                "parameters": {"samples": 25_000, "seed": 7},
            }
        ),
    )
    publisher = CeleryEventPublisher(celery_app, "computegrid.jobs")

    with session_factory() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == created.job.id))
        assert event is not None

    try:
        with start_worker(celery_app, perform_ping_check=False, pool="solo", concurrency=1):
            assert relay_once(session_factory, publisher, batch_size=10) == 1
            publisher.publish(event)
            assert wait_for_terminal(repository, created.job.id) == JobState.SUCCEEDED
    finally:
        get_database_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

    job = repository.get(client_id="e2e-client", job_id=created.job.id)
    assert job is not None
    assert job.result is not None
    assert job.result["samples"] == 25_000
    assert len(job.attempts) == 1
