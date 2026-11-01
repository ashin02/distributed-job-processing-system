"""Celery application and durable job task adapter."""

from __future__ import annotations

import os
import socket
from uuid import UUID

from celery import Celery, Task
from celery.exceptions import Reject

from computegrid_api.config import get_broker_settings, get_lease_settings
from computegrid_api.jobs.execution import (
    ExecutionDisposition,
    execute_job,
    log_execution_timing,
)
from computegrid_api.logging_config import configure_logging

configure_logging("worker")

broker_settings = get_broker_settings()
lease_settings = get_lease_settings()
execution_timing_enabled = os.getenv("COMPUTEGRID_EXECUTION_TIMING_ENABLED", "").lower() in {
    "1",
    "true",
    "yes",
}

celery_app = Celery("computegrid", broker=broker_settings.broker_url)
celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    broker_transport_options={"confirm_publish": True},
    task_acks_on_failure_or_timeout=False,
    task_acks_late=True,
    task_default_exchange="computegrid",
    task_default_exchange_type="direct",
    task_default_queue=broker_settings.job_queue,
    task_default_routing_key=broker_settings.job_queue,
    task_ignore_result=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    worker_prefetch_multiplier=1,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    worker_enable_soft_shutdown_on_idle=True,
    worker_hijack_root_logger=False,
    worker_soft_shutdown_timeout=lease_settings.worker_soft_shutdown_timeout_seconds,
)


def _execute_job_task(task: Task, job_id: str, expected_attempt_number: int = 1) -> str:
    """Execute one message and requeue duplicates until durable completion."""
    request_hostname = getattr(task.request, "hostname", None)
    worker_id = str(request_hostname or socket.gethostname())
    disposition = execute_job(
        UUID(job_id),
        expected_attempt_number=expected_attempt_number,
        worker_id=worker_id,
        timing_sink=log_execution_timing if execution_timing_enabled else None,
    )
    if disposition == ExecutionDisposition.IN_PROGRESS:
        raise Reject("job execution is already in progress", requeue=True)
    return disposition.value


celery_app.task(bind=True, name="computegrid.execute_job", acks_late=True)(_execute_job_task)
