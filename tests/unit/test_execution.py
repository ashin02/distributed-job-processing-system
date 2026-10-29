"""Unit tests for compute execution and Celery acknowledgement settings."""

from computegrid_api.jobs.execution import monte_carlo_pi


def test_monte_carlo_pi_is_deterministic() -> None:
    parameters = {"samples": 10_000, "seed": 42}

    first = monte_carlo_pi(parameters)
    second = monte_carlo_pi(parameters)

    assert first == second
    assert first == {"pi_estimate": 3.126, "samples": 10_000, "seed": 42}


def test_celery_requires_late_acknowledgement_and_publisher_confirms() -> None:
    from computegrid_api.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.broker_transport_options["confirm_publish"] is True
    assert celery_app.conf.task_ignore_result is True
