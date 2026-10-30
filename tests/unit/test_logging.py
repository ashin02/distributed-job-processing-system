"""Tests for the dependency-free structured log formatter."""

import json
import logging

from computegrid_api.logging_config import JsonFormatter


def test_json_formatter_emits_stable_context_fields() -> None:
    record = logging.makeLogRecord(
        {
            "name": "computegrid.test",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "claim committed",
            "args": (),
            "event": "job.claimed",
            "job_id": "job-123",
            "attempt_number": 2,
            "worker_id": "worker-1",
        }
    )

    payload = json.loads(JsonFormatter("worker").format(record))

    assert payload["severity"] == "INFO"
    assert payload["service"] == "worker"
    assert payload["event"] == "job.claimed"
    assert payload["message"] == "claim committed"
    assert payload["job_id"] == "job-123"
    assert payload["attempt_number"] == 2
    assert payload["worker_id"] == "worker-1"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_preserves_exception_context() -> None:
    try:
        raise ValueError("bounded test failure")
    except ValueError as error:
        exception_info = (type(error), error, error.__traceback__)

    record = logging.LogRecord(
        name="computegrid.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="execution failed",
        args=(),
        exc_info=exception_info,
    )

    payload = json.loads(JsonFormatter("worker").format(record))

    assert "ValueError: bounded test failure" in payload["exception"]
