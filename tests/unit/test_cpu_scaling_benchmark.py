"""Tests for CPU-only scaling calculations using the production computation."""

import argparse

import pytest

from scripts.benchmark_cpu_scaling import (
    ScalingResult,
    add_scaling_metrics,
    measure_configuration,
    parse_processes,
)


def result(processes: int, throughput: float) -> ScalingResult:
    return ScalingResult(
        processes=processes,
        jobs=4,
        repeats=1,
        total_jobs=4,
        samples_per_job=100,
        total_samples=400,
        wall_seconds=1,
        worker_process_cpu_seconds=1,
        coordinator_process_cpu_seconds=0.1,
        total_process_cpu_seconds=1.1,
        median_task_wall_seconds=0.25,
        median_task_cpu_seconds=0.25,
        throughput_jobs_per_second=throughput,
        throughput_samples_per_second=throughput * 100,
        speedup=0,
        scaling_efficiency=0,
        average_cpu_cores_used=1.1,
    )


def test_scaling_efficiency_is_relative_to_one_process() -> None:
    measured = add_scaling_metrics([result(1, 4), result(2, 7), result(4, 10)])

    assert measured[0].speedup == 1
    assert measured[0].scaling_efficiency == 1
    assert measured[1].speedup == pytest.approx(1.75)
    assert measured[1].scaling_efficiency == pytest.approx(0.875)
    assert measured[2].speedup == pytest.approx(2.5)
    assert measured[2].scaling_efficiency == pytest.approx(0.625)


def test_cpu_benchmark_runs_exact_production_computation_in_a_child_process() -> None:
    measured = measure_configuration(
        processes=1,
        jobs=2,
        repeats=1,
        samples=10_000,
        seed=42,
    )

    assert measured.total_jobs == 2
    assert measured.total_samples == 20_000
    assert measured.wall_seconds > 0
    assert measured.worker_process_cpu_seconds > 0
    assert measured.throughput_jobs_per_second > 0


def test_process_configuration_requires_unique_one_process_baseline() -> None:
    assert parse_processes("1,2,4") == [1, 2, 4]
    with pytest.raises(argparse.ArgumentTypeError, match="include 1"):
        parse_processes("2,4")
