"""Unit tests for mapping durable outbox events to Celery tasks."""

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from computegrid_api.db.models import OutboxEvent
from computegrid_api.outbox_relay import CeleryEventPublisher


@dataclass
class RecordingTaskSender:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def send_task(self, name: str, **options: Any) -> object:
        self.calls.append((name, options))
        return object()


def test_retry_event_publishes_the_persisted_attempt_generation() -> None:
    sender = RecordingTaskSender()
    event = OutboxEvent(
        id=uuid4(),
        job_id=uuid4(),
        event_type="job.retry_scheduled",
        payload={"job_id": "ignored", "attempt_number": 2},
    )

    CeleryEventPublisher(sender, "computegrid.jobs").publish(event)

    assert sender.calls == [
        (
            "computegrid.execute_job",
            {
                "args": [str(event.job_id), 2],
                "task_id": str(event.id),
                "queue": "computegrid.jobs",
                "routing_key": "computegrid.jobs",
                "retry": False,
                "headers": {"outbox_event_id": str(event.id)},
            },
        )
    ]
