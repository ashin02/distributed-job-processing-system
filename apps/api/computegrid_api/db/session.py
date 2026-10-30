"""SQLAlchemy engine and explicit transaction management."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from computegrid_api.config import get_database_settings

type SessionFactory = sessionmaker[Session]


def create_session_factory(engine: Engine) -> SessionFactory:
    """Create a session factory bound to an engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    """Create and cache the application database engine."""
    settings = get_database_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
    )


@lru_cache
def get_session_factory() -> SessionFactory:
    """Create and cache the application session factory."""
    return create_session_factory(get_engine())


@contextmanager
def transaction(session_factory: SessionFactory | None = None) -> Iterator[Session]:
    """Commit all work atomically, rolling back if the block raises."""
    factory = session_factory or get_session_factory()
    with factory.begin() as session:
        yield session
