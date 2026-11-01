"""Reproducible authenticated API benchmark for fixed Monte Carlo job batches."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class BenchmarkRequestError(RuntimeError):
    """One API request failed or returned an invalid response."""


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    api_url: str
    job_count: int
    samples: int
    submission_concurrency: int
    poll_interval_seconds: float
    timeout_seconds: float
    seed: int
    worker_replicas: int | None
    celery_concurrency: int | None
    cpu_limit: str | None
    memory_limit: str | None


@dataclass(frozen=True, slots=True)
class Submission:
    index: int
    idempotency_key: str
    job_id: UUID | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class JobObservation:
    index: int
    idempotency_key: str
    job_id: str
    state: str
    created_at: datetime | None
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: dict[str, object] | None = None
    poll_error: str | None = None


@dataclass(frozen=True, slots=True)
class LatencyMetric:
    p50_seconds: float | None
    p95_seconds: float | None
    sample_count: int


@dataclass(frozen=True, slots=True)
class PollingMetrics:
    poll_interval_seconds: float
    request_count: int
    request_failures: int
    aggregate_http_seconds: float
    polling_wall_seconds: float


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    requested_jobs: int
    submitted_jobs: int
    terminal_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    incomplete_jobs: int
    submission_failures: int
    failure_rate: float | None
    duration_seconds: float | None
    throughput_jobs_per_second: float | None
    queue_latency: LatencyMetric
    execution_latency: LatencyMetric
    end_to_end_latency: LatencyMetric
    polling: PollingMetrics


class ApiClient:
    """Small standard-library JSON client for the authenticated jobs API."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 30) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._token = token
        self._timeout_seconds = timeout_seconds

    def submit(self, *, idempotency_key: str, samples: int, seed: int) -> UUID:
        response = self._request(
            "POST",
            "v1/jobs",
            body={
                "type": "monte_carlo_pi",
                "parameters": {"samples": samples, "seed": seed},
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        try:
            return UUID(str(response["job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise BenchmarkRequestError("submission response has no valid job_id") from error

    def get_job(self, job_id: UUID) -> dict[str, Any]:
        return self._request("GET", f"v1/jobs/{job_id}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        request_headers.update(headers or {})
        encoded_body = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            encoded_body = json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            urljoin(self._base_url, path),
            data=encoded_body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                payload = json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise BenchmarkRequestError(f"HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise BenchmarkRequestError(str(error)) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BenchmarkRequestError("API returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise BenchmarkRequestError("API returned a non-object JSON response")
        return payload


def percentile(values: list[float], probability: float) -> float | None:
    """Return a linearly interpolated percentile (Hyndman-Fan type 7)."""
    if not values:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def calculate_metrics(
    observations: list[JobObservation],
    *,
    requested_jobs: int,
    submission_failures: int = 0,
    polling: PollingMetrics | None = None,
) -> BenchmarkMetrics:
    """Calculate benchmark metrics from authoritative API timestamps."""
    terminal = [item for item in observations if item.state in TERMINAL_STATES]
    succeeded = sum(item.state == "SUCCEEDED" for item in terminal)
    failed = len(terminal) - succeeded
    queue = _durations(terminal, "created_at", "started_at")
    execution = _durations(terminal, "started_at", "completed_at")
    end_to_end = _durations(terminal, "created_at", "completed_at")

    created = [item.created_at for item in terminal if item.created_at is not None]
    completed = [item.completed_at for item in terminal if item.completed_at is not None]
    duration = None
    throughput = None
    if created and completed:
        duration = (max(completed) - min(created)).total_seconds()
        if duration > 0:
            throughput = len(terminal) / duration

    return BenchmarkMetrics(
        requested_jobs=requested_jobs,
        submitted_jobs=len(observations),
        terminal_jobs=len(terminal),
        succeeded_jobs=succeeded,
        failed_jobs=failed,
        incomplete_jobs=len(observations) - len(terminal),
        submission_failures=submission_failures,
        failure_rate=failed / len(terminal) if terminal else None,
        duration_seconds=duration,
        throughput_jobs_per_second=throughput,
        queue_latency=_latency_metric(queue),
        execution_latency=_latency_metric(execution),
        end_to_end_latency=_latency_metric(end_to_end),
        polling=polling or PollingMetrics(0, 0, 0, 0, 0),
    )


def run_benchmark(
    config: BenchmarkConfig,
    *,
    token: str,
    run_id: str,
) -> tuple[BenchmarkMetrics, list[JobObservation], list[Submission]]:
    client = ApiClient(config.api_url, token)

    def submit(index: int) -> Submission:
        key = f"m5e-a:{run_id}:{index:06d}"
        try:
            job_id = client.submit(idempotency_key=key, samples=config.samples, seed=config.seed)
        except BenchmarkRequestError as error:
            return Submission(index=index, idempotency_key=key, job_id=None, error=str(error))
        return Submission(index=index, idempotency_key=key, job_id=job_id)

    with ThreadPoolExecutor(max_workers=config.submission_concurrency) as executor:
        submissions = list(executor.map(submit, range(config.job_count)))

    accepted = [item for item in submissions if item.job_id is not None]
    observations, polling = _poll_until_terminal(client, accepted, config)
    metrics = calculate_metrics(
        observations,
        requested_jobs=config.job_count,
        submission_failures=len(submissions) - len(accepted),
        polling=polling,
    )
    return metrics, observations, submissions


def _poll_until_terminal(
    client: ApiClient,
    submissions: list[Submission],
    config: BenchmarkConfig,
) -> tuple[list[JobObservation], PollingMetrics]:
    polling_started = monotonic()
    deadline = monotonic() + config.timeout_seconds
    latest: dict[UUID, JobObservation] = {}
    pending = {item.job_id: item for item in submissions if item.job_id is not None}
    last_errors: dict[UUID, str] = {}
    request_count = 0
    request_failures = 0
    aggregate_http_seconds = 0.0

    while pending and monotonic() < deadline:
        def poll(
            item: tuple[UUID, Submission],
        ) -> tuple[UUID, JobObservation | None, str | None, float]:
            job_id, submission = item
            request_started = monotonic()
            try:
                payload = client.get_job(job_id)
                observation = _observation(submission, payload)
                return job_id, observation, None, monotonic() - request_started
            except BenchmarkRequestError as request_error:
                return job_id, None, str(request_error), monotonic() - request_started

        workers = min(config.submission_concurrency, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(poll, pending.items()))
        for job_id, observation, poll_error, request_seconds in results:
            request_count += 1
            aggregate_http_seconds += request_seconds
            if poll_error is not None:
                request_failures += 1
                last_errors[job_id] = poll_error
                continue
            assert observation is not None
            latest[job_id] = observation
            last_errors.pop(job_id, None)
            if observation.state in TERMINAL_STATES:
                pending.pop(job_id)
        if pending:
            sleep(config.poll_interval_seconds)

    for job_id, submission in pending.items():
        if job_id in latest:
            current = latest[job_id]
            latest[job_id] = JobObservation(
                **{**asdict(current), "poll_error": last_errors.get(job_id, "benchmark timeout")}
            )
        else:
            latest[job_id] = JobObservation(
                index=submission.index,
                idempotency_key=submission.idempotency_key,
                job_id=str(job_id),
                state="UNKNOWN",
                created_at=None,
                queued_at=None,
                started_at=None,
                completed_at=None,
                poll_error=last_errors.get(job_id, "benchmark timeout"),
            )
    polling = PollingMetrics(
        poll_interval_seconds=config.poll_interval_seconds,
        request_count=request_count,
        request_failures=request_failures,
        aggregate_http_seconds=aggregate_http_seconds,
        polling_wall_seconds=monotonic() - polling_started,
    )
    return sorted(latest.values(), key=lambda item: item.index), polling


def _observation(submission: Submission, payload: dict[str, Any]) -> JobObservation:
    try:
        return JobObservation(
            index=submission.index,
            idempotency_key=submission.idempotency_key,
            job_id=str(payload["job_id"]),
            state=str(payload["state"]),
            created_at=_parse_datetime(payload.get("created_at")),
            queued_at=_parse_datetime(payload.get("queued_at")),
            started_at=_parse_datetime(payload.get("started_at")),
            completed_at=_parse_datetime(payload.get("completed_at")),
            error=payload.get("error") if isinstance(payload.get("error"), dict) else None,
        )
    except KeyError as error:
        raise BenchmarkRequestError("job response is missing required fields") from error


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BenchmarkRequestError("job response contains a non-string timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BenchmarkRequestError(f"invalid API timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise BenchmarkRequestError("API timestamp has no timezone")
    return parsed.astimezone(UTC)


def _durations(
    observations: list[JobObservation],
    start_field: str,
    end_field: str,
) -> list[float]:
    values: list[float] = []
    for item in observations:
        started = getattr(item, start_field)
        ended = getattr(item, end_field)
        if started is not None and ended is not None:
            values.append(max(0.0, (ended - started).total_seconds()))
    return values


def _latency_metric(values: list[float]) -> LatencyMetric:
    return LatencyMetric(
        p50_seconds=percentile(values, 0.50),
        p95_seconds=percentile(values, 0.95),
        sample_count=len(values),
    )


def export_results(
    output_prefix: Path,
    *,
    run_id: str,
    config: BenchmarkConfig,
    metrics: BenchmarkMetrics,
    observations: list[JobObservation],
    submissions: list[Submission],
) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    generated_at = datetime.now(UTC).isoformat()
    document = {
        "schema_version": 1,
        "benchmark": "M5E-A",
        "run_id": run_id,
        "generated_at": generated_at,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "worker_replicas": config.worker_replicas,
            "celery_concurrency": config.celery_concurrency,
            "cpu_limit": config.cpu_limit,
            "memory_limit": config.memory_limit,
        },
        "config": asdict(config),
        "metrics": asdict(metrics),
        "submission_errors": [asdict(item) for item in submissions if item.error is not None],
        "jobs": [_serialized_observation(item) for item in observations],
    }
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")

    summary = asdict(metrics)
    flat_summary = {key: value for key, value in summary.items() if not isinstance(value, dict)}
    for name in ("queue_latency", "execution_latency", "end_to_end_latency"):
        metric = getattr(metrics, name)
        flat_summary.update(
            {
                f"{name}_p50_seconds": metric.p50_seconds,
                f"{name}_p95_seconds": metric.p95_seconds,
                f"{name}_sample_count": metric.sample_count,
            }
        )
    flat_summary.update(
        {
            "poll_interval_seconds": metrics.polling.poll_interval_seconds,
            "poll_request_count": metrics.polling.request_count,
            "poll_request_failures": metrics.polling.request_failures,
            "poll_aggregate_http_seconds": metrics.polling.aggregate_http_seconds,
            "polling_wall_seconds": metrics.polling.polling_wall_seconds,
        }
    )
    rows = []
    for item in observations:
        serialized = _serialized_observation(item)
        serialized.update(
            {
                "run_id": run_id,
                "worker_replicas": config.worker_replicas,
                "celery_concurrency": config.celery_concurrency,
                "samples": config.samples,
                "queue_latency_seconds": _duration(item.created_at, item.started_at),
                "execution_latency_seconds": _duration(item.started_at, item.completed_at),
                "end_to_end_latency_seconds": _duration(item.created_at, item.completed_at),
                **flat_summary,
            }
        )
        rows.append(serialized)
    for submission in submissions:
        if submission.error is None:
            continue
        rows.append(
            {
                "index": submission.index,
                "idempotency_key": submission.idempotency_key,
                "job_id": "",
                "state": "SUBMISSION_FAILED",
                "error": submission.error,
                "run_id": run_id,
                "worker_replicas": config.worker_replicas,
                "celery_concurrency": config.celery_concurrency,
                "samples": config.samples,
                **flat_summary,
            }
        )
    if not rows:
        rows.append(
            {
                "run_id": run_id,
                "worker_replicas": config.worker_replicas,
                "celery_concurrency": config.celery_concurrency,
                "samples": config.samples,
                **flat_summary,
            }
        )
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with csv_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def _serialized_observation(item: JobObservation) -> dict[str, object]:
    values = asdict(item)
    for key in ("created_at", "queued_at", "started_at", "completed_at"):
        value = values[key]
        values[key] = value.isoformat() if isinstance(value, datetime) else None
    if values["error"] is not None:
        values["error"] = json.dumps(values["error"], sort_keys=True)
    return values


def _duration(started: datetime | None, ended: datetime | None) -> float | None:
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds())


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("COMPUTEGRID_DEVELOPMENT_API_TOKEN"))
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--samples", type=int, default=1_000_000)
    parser.add_argument("--submission-concurrency", type=int, default=8)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-id", default=str(uuid4()))
    parser.add_argument("--output-prefix", type=Path)
    parser.add_argument("--worker-replicas", type=int)
    parser.add_argument("--celery-concurrency", type=int)
    parser.add_argument("--cpu-limit")
    parser.add_argument("--memory-limit")
    args = parser.parse_args(arguments)
    if not args.token:
        parser.error("--token or COMPUTEGRID_DEVELOPMENT_API_TOKEN is required")
    for name in ("jobs", "samples", "submission_concurrency"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.poll_interval <= 0 or args.timeout <= 0:
        parser.error("--poll-interval and --timeout must be positive")
    if args.samples > 10_000_000:
        parser.error("--samples cannot exceed the API maximum of 10000000")
    return args


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    config = BenchmarkConfig(
        api_url=args.api_url,
        job_count=args.jobs,
        samples=args.samples,
        submission_concurrency=args.submission_concurrency,
        poll_interval_seconds=args.poll_interval,
        timeout_seconds=args.timeout,
        seed=args.seed,
        worker_replicas=args.worker_replicas,
        celery_concurrency=args.celery_concurrency,
        cpu_limit=args.cpu_limit,
        memory_limit=args.memory_limit,
    )
    output_prefix = args.output_prefix or Path("benchmark-results") / f"m5e-a-{args.run_id}"
    metrics, observations, submissions = run_benchmark(
        config,
        token=args.token,
        run_id=args.run_id,
    )
    json_path, csv_path = export_results(
        output_prefix,
        run_id=args.run_id,
        config=config,
        metrics=metrics,
        observations=observations,
        submissions=submissions,
    )
    print(json.dumps(asdict(metrics), indent=2))
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")
    complete = (
        metrics.submission_failures == 0
        and metrics.terminal_jobs == metrics.requested_jobs
    )
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
