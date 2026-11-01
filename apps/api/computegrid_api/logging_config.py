"""Small JSON logging configuration shared by ComputeGrid processes."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_COMPUTEGRID_HANDLER = "computegrid-json"
_CONTEXT_FIELDS = (
    "job_id",
    "attempt_number",
    "worker_id",
    "outbox_event_id",
    "disposition",
    "recovered",
    "retries_scheduled",
    "terminal_failures",
    "stale_attempts",
    "claim_seconds",
    "computation_seconds",
    "finalize_seconds",
    "persistence_seconds",
    "overhead_seconds",
    "total_seconds",
)


class JsonFormatter(logging.Formatter):
    """Render stable application fields as one JSON object per line."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "service": self._service,
            "event": record.__dict__.get("event", record.name),
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = record.__dict__.get(field)
            if value is not None:
                payload[field] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(service: str) -> None:
    """Install one stdout JSON handler without multiplying handlers on reconfiguration."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    owned_handlers = [
        handler
        for handler in root_logger.handlers
        if handler.get_name() == _COMPUTEGRID_HANDLER
    ]
    if owned_handlers:
        for handler in owned_handlers:
            handler.setFormatter(JsonFormatter(service))
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter(service))
        handler.set_name(_COMPUTEGRID_HANDLER)
        root_logger.addHandler(handler)

    # Uvicorn otherwise keeps separate text handlers and duplicates application output.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        process_logger = logging.getLogger(logger_name)
        process_logger.handlers.clear()
        process_logger.propagate = True
