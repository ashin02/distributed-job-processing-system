"""PostgreSQL fixtures for persistence integration tests."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from computegrid_api.db.session import SessionFactory, create_session_factory
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


class IntegrationSettings(BaseSettings):
    """Settings used only by database integration tests."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPUTEGRID_", extra="ignore")

    test_database_url: str = Field(min_length=1)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Return a guarded URL for the dedicated integration-test database."""
    database_url = IntegrationSettings().test_database_url  # type: ignore[call-arg]

    database_name = make_url(database_url).database
    if database_name is None or not database_name.endswith("_test"):
        pytest.fail("The integration database name must end with '_test'")
    return database_url


@pytest.fixture(scope="session")
def migrated_engine(test_database_url: str) -> Iterator[Engine]:
    """Migrate an empty test schema to head and remove it after the test session."""
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", test_database_url)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(test_database_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(alembic_config, "base")


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Iterator[SessionFactory]:
    """Provide sessions and clean all persistence tables after each test."""
    factory = create_session_factory(migrated_engine)
    try:
        yield factory
    finally:
        with Session(migrated_engine) as session, session.begin():
            session.execute(
                text("TRUNCATE demo_runs, demo_rate_limits, " \
                     "outbox_events, job_attempts, jobs CASCADE")
            )
