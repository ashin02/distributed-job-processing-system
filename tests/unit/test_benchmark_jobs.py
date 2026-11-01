"""Metric calculation tests for the M5E-A benchmark harness."""

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from scripts.benchmark_jobs import (
    BenchmarkConfig,
    JobObservation,
    Submission,
    calculate_metrics,
    export_results,
    percentile,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def observation(
    index: int,
    *,
    state: str = "SUCCEEDED",
    queue_seconds: float = 1,
    execution_seconds: float = 2,
    missing_started_at: bool = False,
) -> JobObservation:
    started_at = None if missing_started_at else START + timedelta(seconds=queue_seconds)
    completed_at = START + timedelta(seconds=queue_seconds + execution_seconds)
    return JobObservation(
        index=index,
        idempotency_key=f"run:{index}",
        job_id=str(index),
        state=state,
        created_at=START,
        queued_at=START + timedelta(seconds=0.1),
        started_at=started_at,
        completed_at=completed_at,
    )


def test_percentile_and_metric_calculations() -> None:
    jobs = [
        observation(1, queue_seconds=1, execution_seconds=2),
        observation(2, queue_seconds=2, execution_seconds=4),
        observation(3, queue_seconds=3, execution_seconds=6),
    ]

    metrics = calculate_metrics(jobs, requested_jobs=3)

    assert percentile([1, 2, 3], 0.50) == 2
    assert percentile([1, 2, 3], 0.95) == pytest.approx(2.9)
    assert metrics.queue_latency.p50_seconds == 2
    assert metrics.queue_latency.p95_seconds == pytest.approx(2.9)
    assert metrics.execution_latency.p50_seconds == 4
    assert metrics.end_to_end_latency.p50_seconds == 6
    assert metrics.duration_seconds == 9
    assert metrics.throughput_jobs_per_second == pytest.approx(1 / 3)
    assert metrics.failure_rate == 0


def test_partial_failures_and_missing_timestamps_are_reported() -> None:
    jobs = [
        observation(1),
        observation(2, state="FAILED", execution_seconds=3),
        observation(3, state="RUNNING", missing_started_at=True),
        observation(4, state="CANCELLED", missing_started_at=True),
    ]

    metrics = calculate_metrics(jobs, requested_jobs=5, submission_failures=1)

    assert metrics.submitted_jobs == 4
    assert metrics.terminal_jobs == 3
    assert metrics.succeeded_jobs == 1
    assert metrics.failed_jobs == 2
    assert metrics.incomplete_jobs == 1
    assert metrics.submission_failures == 1
    assert metrics.failure_rate == pytest.approx(2 / 3)
    assert metrics.queue_latency.sample_count == 2
    assert metrics.execution_latency.sample_count == 2
    assert metrics.end_to_end_latency.sample_count == 3


def test_json_and_csv_exports_include_partial_submission_failures(tmp_path: Path) -> None:
    output = tmp_path / "result"
    jobs = [observation(1)]
    metrics = calculate_metrics(jobs, requested_jobs=2, submission_failures=1)
    config = BenchmarkConfig(
        api_url="http://api.invalid",
        job_count=2,
        samples=100,
        submission_concurrency=2,
        poll_interval_seconds=0.25,
        timeout_seconds=60,
        seed=42,
        worker_replicas=1,
        celery_concurrency=1,
        cpu_limit="none",
        memory_limit="none",
    )
    submissions = [
        Submission(1, "run:1", UUID(int=1)),
        Submission(2, "run:2", None, "HTTP 503"),
    ]

    json_path, csv_path = export_results(
        output,
        run_id="run",
        config=config,
        metrics=metrics,
        observations=jobs,
        submissions=submissions,
    )

    document = json.loads(json_path.read_text())
    assert document["metrics"]["submission_failures"] == 1
    assert document["submission_errors"][0]["error"] == "HTTP 503"
    with csv_path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert [row["state"] for row in rows] == ["SUCCEEDED", "SUBMISSION_FAILED"]
    assert rows[0]["queue_latency_p95_seconds"] == "1.0"
