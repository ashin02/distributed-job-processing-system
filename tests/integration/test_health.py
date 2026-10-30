"""PostgreSQL integration coverage for API readiness."""

import pytest
from computegrid_api.db.session import SessionFactory
from computegrid_api.main import readyz

pytestmark = pytest.mark.integration


def test_readyz_checks_migrated_postgresql(session_factory: SessionFactory) -> None:
    response = readyz(session_factory)

    assert response.status == "ready"
    assert response.database == "ok"
