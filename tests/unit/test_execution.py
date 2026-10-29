"""Unit tests for compute execution, retry timing, and Celery acknowledgement settings."""

import pytest
from computegrid_api.jobs.execution import RetryPolicy, monte_carlo_pi


def test_monte_carlo_pi_is_deterministic() -> None:
    parameters = {"samples": 10_000, "seed": 42}

    first = monte_carlo_pi(parameters)
    second = monte_carlo_pi(parameters)

    assert first == second
    assert first == {"pi_estimate": 3.126, "samples": 10_000, "seed": 42}


@pytest.mark.parametrize(
    ("attempt_number", "random_value", "expected"),
    [
        (1, 0.0, 5.0),
        (1, 1.0, 10.0),
        (2, 0.0, 10.0),
        (2, 1.0, 20.0),
        (3, 0.0, 12.5),
        (3, 1.0, 25.0),
    ],
)
def test_retry_policy_uses_bounded_exponential_backoff_with_jitter(
    attempt_number: int, random_value: float, expected: float
) -> None:
    policy = RetryPolicy(
        max_attempts=4,
        base_delay_seconds=10,
        max_delay_seconds=25,
        jitter_ratio=0.5,
    )

    assert policy.delay_seconds(attempt_number, random_value=random_value) == expected


def test_celery_requires_late_acknowledgement_and_publisher_confirms() -> None:
    from computegrid_api.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.worker_cancel_long_running_tasks_on_connection_loss is True
    assert celery_app.conf.worker_enable_soft_shutdown_on_idle is True
    assert celery_app.conf.worker_soft_shutdown_timeout >= 0
    assert celery_app.conf.broker_transport_options["confirm_publish"] is True
    assert celery_app.conf.task_ignore_result is True
