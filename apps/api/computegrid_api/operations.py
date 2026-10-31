"""Read-only operational snapshots from PostgreSQL, RabbitMQ, and Celery."""

from __future__ import annotations

import json
import re
from base64 import b64encode
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from hmac import new as new_hmac
from json import JSONDecodeError
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from celery import Celery
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only, selectinload

from computegrid_api.config import BrokerSettings, OperationsSettings
from computegrid_api.db.models import AttemptState, Job, JobAttempt, JobState
from computegrid_api.db.session import SessionFactory

_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


class DependencyStatus(StrEnum):
    """Availability state for one independently queried dependency."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class WorkerActivity(StrEnum):
    """Whether a responding Celery worker currently reports active tasks."""

    IDLE = "IDLE"
    BUSY = "BUSY"
    UNKNOWN = "UNKNOWN"


class RabbitMQSummary(BaseModel):
    """Actual counters returned by the RabbitMQ management API."""

    status: DependencyStatus
    queue_name: str
    messages_ready: int | None = None
    messages_unacknowledged: int | None = None
    consumer_count: int | None = None
    message: str | None = None


class WorkerSummary(BaseModel):
    """One Celery worker observed through the control inspection API."""

    worker_id: str
    availability: DependencyStatus
    activity: WorkerActivity
    current_job_id: UUID | None = None


class WorkersSummary(BaseModel):
    """Celery inspection status and all workers that answered in time."""

    status: DependencyStatus
    items: list[WorkerSummary]
    message: str | None = None


class PostgreSQLSummary(BaseModel):
    """Authoritative job counts grouped by durable state."""

    status: DependencyStatus
    job_counts: dict[JobState, int] | None
    message: str | None = None


class VisibleJobSummary(BaseModel):
    """One caller-visible job and its latest persisted execution evidence."""

    job_id: UUID
    job_type: str
    state: JobState
    attempt_state: AttemptState | None = None
    worker_key: str | None = None


class OperationsSummary(BaseModel):
    """Point-in-time, read-only operational snapshot."""

    generated_at: datetime
    rabbitmq: RabbitMQSummary
    workers: WorkersSummary
    postgresql: PostgreSQLSummary
    visible_jobs_status: DependencyStatus = DependencyStatus.AVAILABLE
    visible_jobs: list[VisibleJobSummary] = Field(default_factory=list)


class PublicRabbitMQSummary(BaseModel):
    """Queue counters safe to expose without infrastructure identifiers."""

    status: DependencyStatus
    messages_ready: int | None = None
    messages_unacknowledged: int | None = None
    consumer_count: int | None = None
    message: str | None = None


class PublicWorkerSummary(BaseModel):
    """One discovered worker with an anonymous-safe display label."""

    label: str
    worker_key: str
    availability: DependencyStatus
    activity: WorkerActivity


class PublicWorkersSummary(BaseModel):
    """Aggregate worker capacity without worker or active-job identities."""

    status: DependencyStatus
    items: list[PublicWorkerSummary]
    available: int
    idle: int
    busy: int
    message: str | None = None


class PublicOperationsSummary(BaseModel):
    """Anonymous-safe operational snapshot."""

    generated_at: datetime
    rabbitmq: PublicRabbitMQSummary
    workers: PublicWorkersSummary
    postgresql: PostgreSQLSummary
    visible_jobs_status: DependencyStatus
    visible_jobs: list[VisibleJobSummary]


def _public_worker_key(worker_id: str, secret: str) -> str:
    digest = new_hmac(secret.encode(), f"worker:{worker_id}".encode(), sha256).hexdigest()
    return f"worker-{digest[:12]}"


def public_operations_summary(
    summary: OperationsSummary,
    *,
    worker_key_secret: str,
) -> PublicOperationsSummary:
    """Remove infrastructure and cross-visitor identifiers from a snapshot."""
    available = sum(
        worker.availability == DependencyStatus.AVAILABLE for worker in summary.workers.items
    )
    idle = sum(worker.activity == WorkerActivity.IDLE for worker in summary.workers.items)
    busy = sum(worker.activity == WorkerActivity.BUSY for worker in summary.workers.items)
    return PublicOperationsSummary(
        generated_at=summary.generated_at,
        rabbitmq=PublicRabbitMQSummary(
            status=summary.rabbitmq.status,
            messages_ready=summary.rabbitmq.messages_ready,
            messages_unacknowledged=summary.rabbitmq.messages_unacknowledged,
            consumer_count=summary.rabbitmq.consumer_count,
            message=summary.rabbitmq.message,
        ),
        workers=PublicWorkersSummary(
            status=summary.workers.status,
            items=[
                PublicWorkerSummary(
                    label=f"Worker {worker_key[-4:].upper()}",
                    worker_key=worker_key,
                    availability=worker.availability,
                    activity=worker.activity,
                )
                for worker in summary.workers.items
                for worker_key in [_public_worker_key(worker.worker_id, worker_key_secret)]
            ],
            available=available,
            idle=idle,
            busy=busy,
            message=summary.workers.message,
        ),
        postgresql=summary.postgresql,
        visible_jobs_status=summary.visible_jobs_status,
        visible_jobs=[
            job.model_copy(
                update={
                    "worker_key": _public_worker_key(job.worker_key, worker_key_secret)
                    if job.worker_key is not None
                    else None
                }
            )
            for job in summary.visible_jobs
        ],
    )


class OperationsService:
    """Collect independently degradable operational data with bounded remote calls."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        broker_settings: BrokerSettings,
        operations_settings: OperationsSettings,
    ) -> None:
        self._session_factory = session_factory
        self._broker_settings = broker_settings
        self._operations_settings = operations_settings

    def summary(
            self,
            *,
            client_id: str | None = None,
            flow_only: bool = False,
        ) -> OperationsSummary:
        """Return fresh data without mutating any dependency."""
        timeout = self._operations_settings.operations_timeout_seconds
        visible_jobs_status, visible_jobs = visitor_jobs_summary(
            self._session_factory,
            client_id=client_id,
            timeout_seconds=timeout,
        )
        return OperationsSummary(
            generated_at=datetime.now(UTC),
            rabbitmq=RabbitMQSummary(status=DependencyStatus.UNKNOWN, queue_name="")
            if flow_only else rabbitmq_summary(
                self._broker_settings,
                self._operations_settings.rabbitmq_management_url,
                timeout=timeout,
            ),
            workers=WorkersSummary(status=DependencyStatus.UNKNOWN, items=[])
            if flow_only else celery_summary(self._broker_settings, timeout=timeout),
            postgresql=PostgreSQLSummary(status=DependencyStatus.UNKNOWN, job_counts=None)
            if flow_only else postgresql_summary(
                self._session_factory,
                timeout_seconds=timeout,
            ),
            visible_jobs_status=visible_jobs_status,
            visible_jobs=visible_jobs,
        )


def visitor_jobs_summary(
    session_factory: SessionFactory,
    *,
    client_id: str | None,
    limit: int = 100,
    timeout_seconds: float = 2.0,
) -> tuple[DependencyStatus, list[VisibleJobSummary]]:
    """Return only one authorized client's jobs with persisted attempt correlation."""
    if client_id is None:
        return DependencyStatus.AVAILABLE, []
    statement = (
        select(Job)
        .options(
            load_only(Job.id, Job.job_type, Job.state),
            selectinload(Job.attempts).load_only(
                JobAttempt.id, JobAttempt.state, JobAttempt.worker_id,
            ),
        )
        .where(Job.client_id == client_id)
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(limit)
    )
    try:
        with session_factory() as session:
            session.execute(
                text("SELECT set_config('statement_timeout', :timeout_ms, true)"),
                {"timeout_ms": str(max(1, int(timeout_seconds * 1000)))},
            )
            jobs = list(session.scalars(statement))
    except SQLAlchemyError:
        return DependencyStatus.UNAVAILABLE, []

    visible: list[VisibleJobSummary] = []
    for job in jobs:
        attempt = job.attempts[-1] if job.attempts else None
        running_worker = (
            attempt.worker_id
            if job.state == JobState.RUNNING
            and attempt is not None
            and attempt.state == AttemptState.RUNNING
            else None
        )
        visible.append(
            VisibleJobSummary(
                job_id=job.id,
                job_type=job.job_type,
                state=job.state,
                attempt_state=attempt.state if attempt is not None else None,
                worker_key=running_worker,
            )
        )
    return DependencyStatus.AVAILABLE, visible


def rabbitmq_summary(
    broker_settings: BrokerSettings,
    management_url: str,
    *,
    timeout: float,
) -> RabbitMQSummary:
    """Read queue counters from RabbitMQ's bounded management HTTP endpoint."""
    queue_name = broker_settings.job_queue
    broker_url = urlparse(broker_settings.broker_url)
    username = unquote(broker_url.username or "")
    password = unquote(broker_url.password or "")
    if not username or not password:
        return RabbitMQSummary(
            status=DependencyStatus.UNKNOWN,
            queue_name=queue_name,
            message="RabbitMQ credentials are not configured",
        )

    virtual_host = unquote(broker_url.path[1:]) if broker_url.path.startswith("/") else "/"
    if not virtual_host:
        virtual_host = "/"
    endpoint = (
        f"{management_url.rstrip('/')}/api/queues/"
        f"{quote(virtual_host, safe='')}/{quote(queue_name, safe='')}"
    )
    credentials = b64encode(f"{username}:{password}".encode()).decode("ascii")
    try:
        request = Request(
            endpoint,
            headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
        )
    except ValueError:
        return RabbitMQSummary(
            status=DependencyStatus.UNKNOWN,
            queue_name=queue_name,
            message="RabbitMQ management URL is invalid",
        )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            return RabbitMQSummary(
                status=DependencyStatus.AVAILABLE,
                queue_name=queue_name,
                messages_ready=0,
                messages_unacknowledged=0,
                consumer_count=0,
                message="Queue has not been declared",
            )
        return RabbitMQSummary(
            status=DependencyStatus.UNAVAILABLE,
            queue_name=queue_name,
            message="RabbitMQ management API is unavailable",
        )
    except OSError:
        return RabbitMQSummary(
            status=DependencyStatus.UNAVAILABLE,
            queue_name=queue_name,
            message="RabbitMQ management API is unavailable",
        )
    except (JSONDecodeError, UnicodeDecodeError):
        return RabbitMQSummary(
            status=DependencyStatus.UNKNOWN,
            queue_name=queue_name,
            message="RabbitMQ returned an invalid response",
        )

    if not isinstance(payload, dict):
        return RabbitMQSummary(
            status=DependencyStatus.UNKNOWN,
            queue_name=queue_name,
            message="RabbitMQ returned an invalid response",
        )
    counters = (
        _nonnegative_int(payload.get("messages_ready")),
        _nonnegative_int(payload.get("messages_unacknowledged")),
        _nonnegative_int(payload.get("consumers")),
    )
    if any(counter is None for counter in counters):
        return RabbitMQSummary(
            status=DependencyStatus.UNKNOWN,
            queue_name=queue_name,
            message="RabbitMQ queue counters are unavailable",
        )
    return RabbitMQSummary(
        status=DependencyStatus.AVAILABLE,
        queue_name=queue_name,
        messages_ready=counters[0],
        messages_unacknowledged=counters[1],
        consumer_count=counters[2],
    )


def celery_summary(broker_settings: BrokerSettings, *, timeout: float) -> WorkersSummary:
    """Inspect active tasks; workers that answer are known to be available."""
    try:
        active_by_worker = _inspect_active(broker_settings.broker_url, timeout)
    except Exception:
        return WorkersSummary(
            status=DependencyStatus.UNAVAILABLE,
            items=[],
            message="Celery control inspection is unavailable",
        )
    if active_by_worker is None:
        return WorkersSummary(
            status=DependencyStatus.UNAVAILABLE,
            items=[],
            message="No Celery workers responded before the timeout",
        )
    if not isinstance(active_by_worker, dict):
        return WorkersSummary(
            status=DependencyStatus.UNKNOWN,
            items=[],
            message="Celery returned an invalid inspection response",
        )
    if not active_by_worker:
        return WorkersSummary(
            status=DependencyStatus.UNAVAILABLE,
            items=[],
            message="No Celery workers responded before the timeout",
        )
    if any(
        not isinstance(worker_id, str) or not isinstance(active_tasks, list)
        for worker_id, active_tasks in active_by_worker.items()
    ):
        return WorkersSummary(
            status=DependencyStatus.UNKNOWN,
            items=[],
            message="Celery returned an invalid inspection response",
        )

    workers: list[WorkerSummary] = []
    for worker_id, active_tasks in sorted(active_by_worker.items()):
        workers.append(
            WorkerSummary(
                worker_id=worker_id,
                availability=DependencyStatus.AVAILABLE,
                activity=WorkerActivity.BUSY if active_tasks else WorkerActivity.IDLE,
                current_job_id=_current_job_id(active_tasks),
            )
        )
    return WorkersSummary(status=DependencyStatus.AVAILABLE, items=workers)


def postgresql_summary(
    session_factory: SessionFactory,
    *,
    timeout_seconds: float,
) -> PostgreSQLSummary:
    """Count authoritative jobs by state with a PostgreSQL statement timeout."""
    counts = {state: 0 for state in JobState}
    try:
        with session_factory() as session:
            session.execute(
                text("SELECT set_config('statement_timeout', :timeout_ms, true)"),
                {"timeout_ms": str(max(1, int(timeout_seconds * 1000)))},
            )
            rows = session.execute(select(Job.state, func.count(Job.id)).group_by(Job.state))
            for state, count in rows:
                counts[JobState(state)] = int(count)
    except SQLAlchemyError:
        return PostgreSQLSummary(
            status=DependencyStatus.UNAVAILABLE,
            job_counts=None,
            message="PostgreSQL is unavailable",
        )
    return PostgreSQLSummary(status=DependencyStatus.AVAILABLE, job_counts=counts)


def _inspect_active(broker_url: str, timeout: float) -> object:
    app = Celery("computegrid-operations", broker=broker_url)
    app.conf.broker_connection_retry = False
    app.conf.broker_connection_timeout = timeout
    app.conf.broker_connection_max_retries = 0
    app.conf.broker_transport_options = {"max_retries": 0}
    result: object = app.control.inspect(timeout=timeout).active()
    return result


def _current_job_id(active_tasks: list[object]) -> UUID | None:
    for task in active_tasks:
        if not isinstance(task, dict) or task.get("name") != "computegrid.execute_job":
            continue
        args: Any = task.get("args")
        if isinstance(args, list | tuple) and args:
            try:
                return UUID(str(args[0]))
            except ValueError:
                continue
        if isinstance(args, str) and (match := _UUID_PATTERN.search(args)) is not None:
            return UUID(match.group())
    return None


def _nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value
