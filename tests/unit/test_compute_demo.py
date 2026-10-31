"""Preset validation and browser admission boundaries without executing solvers."""

from typing import cast

import pytest
from computegrid_api.api.auth import AuthenticatedClient
from computegrid_api.api.demo_runs import access
from computegrid_api.compute_demo import presets
from computegrid_api.config import BrokerSettings, ComputeDemoSettings
from computegrid_api.db.session import SessionFactory
from fastapi import HTTPException
from starlette.requests import Request


def test_presets_are_deterministic_bounded_and_cover_existing_jobs() -> None:
    settings = ComputeDemoSettings( # type: ignore[call-arg]
        _env_file=None, compute_demo_samples=10_000_000, compute_demo_variables=18)
    first = presets(settings)
    assert first == presets(settings)
    assert [p.type.value for p in first] == ["monte_carlo_pi", "weighted_max_cut", "bqm", "cqm"]
    assert first[0].parameters.model_dump()["samples"] == 10_000_000
    max_cut = first[1].parameters.model_dump()
    assert len(max_cut["nodes"]) == 18
    assert len(max_cut["edges"]) == 153
    bqm = first[2].parameters.model_dump()
    assert len(bqm["variables"]) == 18
    assert len(bqm["linear"]) == 18
    assert len(bqm["quadratic"]) == 153
    cqm = first[3].parameters.model_dump()
    assert len(cqm["variables"]) == 18
    assert len(cqm["objective"]["linear"]) == 18
    assert len(cqm["constraints"]) == 18
    assert all(len(constraint["linear"]) == 18 for constraint in cqm["constraints"])


def test_submission_defaults_support_the_twenty_second_feed() -> None:
    settings = ComputeDemoSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.compute_demo_interval_seconds == 0.5
    assert settings.compute_demo_max_jobs == 40
    assert settings.compute_demo_max_feeds == 1
    assert settings.compute_demo_active_capacity == 40
    broker = BrokerSettings(_env_file=None)  # type: ignore[call-arg]
    assert broker.outbox_poll_interval_seconds == 0.5


@pytest.mark.parametrize("origin", [None, "https://untrusted.invalid", "null"])
def test_anonymous_start_requires_allowed_origin(origin: str | None) -> None:
    request = Request({"type": "http", "method": "POST", "headers":
                       [] if origin is None else [(b"origin", origin.encode())]})
    with pytest.raises(HTTPException) as error:
        access(request, AuthenticatedClient("visitor"), cast(SessionFactory, None))
    assert error.value.status_code == 403
