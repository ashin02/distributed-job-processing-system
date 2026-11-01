"""Diagnose CPU scaling with the production Monte Carlo computation unchanged."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter, process_time

from computegrid_api.jobs.execution import monte_carlo_pi


@dataclass(frozen=True, slots=True)
class TaskTiming:
    wall_seconds: float
    process_cpu_seconds: float
    pi_estimate: float


@dataclass(frozen=True, slots=True)
class ScalingResult:
    processes: int
    jobs: int
    repeats: int
    total_jobs: int
    samples_per_job: int
    total_samples: int
    wall_seconds: float
    worker_process_cpu_seconds: float
    coordinator_process_cpu_seconds: float
    total_process_cpu_seconds: float
    median_task_wall_seconds: float
    median_task_cpu_seconds: float
    throughput_jobs_per_second: float
    throughput_samples_per_second: float
    speedup: float
    scaling_efficiency: float
    average_cpu_cores_used: float


def _run_exact_computation(samples: int, seed: int) -> TaskTiming:
    """Run the production implementation and return child-process timings."""
    wall_started = perf_counter()
    cpu_started = process_time()
    result = monte_carlo_pi({"samples": samples, "seed": seed})
    process_cpu_seconds = process_time() - cpu_started
    wall_seconds = perf_counter() - wall_started
    return TaskTiming(
        wall_seconds=wall_seconds,
        process_cpu_seconds=process_cpu_seconds,
        pi_estimate=float(result["pi_estimate"]),
    )


def measure_configuration(
    *,
    processes: int,
    jobs: int,
    repeats: int,
    samples: int,
    seed: int,
) -> ScalingResult:
    """Measure one process count, including pool dispatch and lifecycle overhead."""
    total_jobs = jobs * repeats
    coordinator_started = process_time()
    wall_started = perf_counter()
    with ProcessPoolExecutor(max_workers=processes) as executor:
        timings = list(
            executor.map(
                _run_exact_computation,
                [samples] * total_jobs,
                [seed] * total_jobs,
            )
        )
    wall_seconds = perf_counter() - wall_started
    coordinator_cpu_seconds = process_time() - coordinator_started
    if len({timing.pi_estimate for timing in timings}) != 1:
        raise RuntimeError("fixed Monte Carlo workload produced inconsistent results")
    worker_cpu_seconds = sum(timing.process_cpu_seconds for timing in timings)
    total_cpu_seconds = worker_cpu_seconds + coordinator_cpu_seconds
    return ScalingResult(
        processes=processes,
        jobs=jobs,
        repeats=repeats,
        total_jobs=total_jobs,
        samples_per_job=samples,
        total_samples=samples * total_jobs,
        wall_seconds=wall_seconds,
        worker_process_cpu_seconds=worker_cpu_seconds,
        coordinator_process_cpu_seconds=coordinator_cpu_seconds,
        total_process_cpu_seconds=total_cpu_seconds,
        median_task_wall_seconds=median(timing.wall_seconds for timing in timings),
        median_task_cpu_seconds=median(timing.process_cpu_seconds for timing in timings),
        throughput_jobs_per_second=total_jobs / wall_seconds,
        throughput_samples_per_second=(samples * total_jobs) / wall_seconds,
        speedup=0,
        scaling_efficiency=0,
        average_cpu_cores_used=total_cpu_seconds / wall_seconds,
    )


def add_scaling_metrics(results: list[ScalingResult]) -> list[ScalingResult]:
    """Calculate speedup and efficiency relative to the one-process result."""
    try:
        baseline = next(result for result in results if result.processes == 1)
    except StopIteration as error:
        raise ValueError("process configurations must include one process") from error
    return [
        replace(
            result,
            speedup=result.throughput_jobs_per_second / baseline.throughput_jobs_per_second,
            scaling_efficiency=(
                result.throughput_jobs_per_second
                / baseline.throughput_jobs_per_second
                / result.processes
            ),
        )
        for result in results
    ]


def export_results(
    output_prefix: Path,
    *,
    results: list[ScalingResult],
    processes: list[int],
    jobs: int,
    repeats: int,
    samples: int,
    seed: int,
) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    document = {
        "schema_version": 1,
        "benchmark": "M5E-D",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
        },
        "config": {
            "processes": processes,
            "jobs": jobs,
            "repeats": repeats,
            "samples": samples,
            "seed": seed,
        },
        "results": [asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    with csv_path.open("w", newline="") as output:
        rows = [asdict(result) for result in results]
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def parse_processes(value: str) -> list[int]:
    try:
        processes = [int(item.strip()) for item in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("processes must be comma-separated integers") from error
    if not processes or any(item < 1 for item in processes):
        raise argparse.ArgumentTypeError("process counts must be positive")
    if len(set(processes)) != len(processes):
        raise argparse.ArgumentTypeError("process counts must be unique")
    if 1 not in processes:
        raise argparse.ArgumentTypeError("process counts must include 1 for the baseline")
    return processes


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=parse_processes, default=parse_processes("1,2,4"))
    parser.add_argument("--jobs", type=int, default=8, help="jobs per repeat")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=10_000_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=Path("benchmark-results/m5e-d-cpu-scaling"),
    )
    args = parser.parse_args(arguments)
    if args.jobs < 1 or args.repeats < 1:
        parser.error("--jobs and --repeats must be positive")
    if not 1 <= args.samples <= 10_000_000:
        parser.error("--samples must be between 1 and 10000000")
    return args


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    raw_results = [
        measure_configuration(
            processes=processes,
            jobs=args.jobs,
            repeats=args.repeats,
            samples=args.samples,
            seed=args.seed,
        )
        for processes in args.processes
    ]
    results = add_scaling_metrics(raw_results)
    json_path, csv_path = export_results(
        args.output_prefix,
        results=results,
        processes=args.processes,
        jobs=args.jobs,
        repeats=args.repeats,
        samples=args.samples,
        seed=args.seed,
    )
    print(json.dumps([asdict(result) for result in results], indent=2))
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
