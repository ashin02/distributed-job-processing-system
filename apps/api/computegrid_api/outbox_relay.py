"""Relay committed outbox events to Celery with RabbitMQ publisher confirms."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select

from computegrid_api.compute_demo import advance_feeds
from computegrid_api.config import (
    BrokerSettings,
    get_broker_settings,
    get_compute_demo_settings,
    get_lease_settings,
)
from computegrid_api.db.models import Job, JobState, OutboxEvent
from computegrid_api.db.session import SessionFactory, get_session_factory, transaction
from computegrid_api.jobs.recovery import recover_expired_leases
from computegrid_api.logging_config import configure_logging

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    """Confirmed publication boundary used by the relay."""

    def publish(self, event: OutboxEvent) -> None:
        """Publish an event or raise if the broker did not confirm it."""


class TaskSender(Protocol):
    """Subset of the Celery API needed by the confirmed publisher."""

    def send_task(
        self,
        name: str,
        *,
        args: list[object],
        task_id: str,
        queue: str,
        routing_key: str,
        retry: bool,
        headers: dict[str, str],
    ) -> object: ...


class CeleryEventPublisher:
    """Publish Celery tasks through a confirm-enabled broker connection."""

    def __init__(self, app: TaskSender, queue: str) -> None:
        self._app = app
        self._queue = queue

    def publish(self, event: OutboxEvent) -> None:
        if event.event_type not in {"job.accepted", "job.retry_scheduled"}:
            raise ValueError(f"unsupported outbox event type: {event.event_type}")
        attempt_number = event.payload.get("attempt_number", 1)
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 1
        ):
            raise ValueError(f"outbox event {event.id} has an invalid attempt number")
        self._app.send_task(
            "computegrid.execute_job",
            args=[str(event.job_id), attempt_number],
            task_id=str(event.id),
            queue=self._queue,
            routing_key=self._queue,
            retry=False,
            headers={"outbox_event_id": str(event.id)},
        )


def relay_once(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    *,
    batch_size: int,
) -> int:
    """Publish one locked batch and return the confirmed event count."""
    now = datetime.now(UTC)
    confirmed = 0
    queued_jobs: list[tuple[str, int]] = []
    with transaction(session_factory) as session:
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            job = session.scalar(select(Job).where(Job.id == event.job_id).with_for_update())
            if job is None:
                raise RuntimeError(f"outbox event {event.id} references a missing job")
            event.publish_attempts += 1
            try:
                publisher.publish(event)
            except Exception as error:
                event.last_error = str(error)[:2000]
                event.next_attempt_at = now + timedelta(seconds=5)
                logger.warning(
                    "Outbox publication failed",
                    extra={
                        "event": "outbox.publish_failed",
                        "job_id": str(event.job_id),
                        "outbox_event_id": str(event.id),
                    },
                    exc_info=True,
                )
                continue

            event.published_at = now
            event.next_attempt_at = None
            event.last_error = None
            if (
                (event.event_type == "job.accepted" and job.state == JobState.ACCEPTED)
                or (
                    event.event_type == "job.retry_scheduled"
                    and job.state == JobState.RETRY_WAIT
                )
            ):
                job.state = JobState.QUEUED
                job.queued_at = job.queued_at or now
                queued_jobs.append(
                    (str(job.id), int(event.payload.get("attempt_number", 1)))
                )
            confirmed += 1
    for job_id, attempt_number in queued_jobs:
        logger.info(
            "Confirmed message publication moved job to queued",
            extra={
                "event": "job.queued",
                "job_id": job_id,
                "attempt_number": attempt_number,
            },
        )
    return confirmed


def run(settings: BrokerSettings | None = None) -> None:
    """Poll continuously until the process is terminated."""
    from computegrid_api.celery_app import celery_app

    configure_logging("outbox-relay")
    active_settings = settings or get_broker_settings()
    lease_settings = get_lease_settings()
    publisher = CeleryEventPublisher(celery_app, active_settings.job_queue)
    session_factory = get_session_factory()
    logger.info("Outbox relay started", extra={"event": "relay.started"})
    while True:
        advance_feeds(session_factory, get_compute_demo_settings())
        recovery = recover_expired_leases(
            session_factory,
            batch_size=lease_settings.lease_recovery_batch_size,
        )
        published = relay_once(
            session_factory,
            publisher,
            batch_size=active_settings.outbox_batch_size,
        )
        if published == 0 and recovery.recovered == 0 and recovery.stale_attempts == 0:
            time.sleep(active_settings.outbox_poll_interval_seconds)


if __name__ == "__main__":
    run()
