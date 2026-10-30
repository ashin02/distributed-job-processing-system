"""PostgreSQL-backed anonymous-demo rate limiting and workload admission."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from computegrid_api.db.models import DemoRateLimit as DemoRateLimitRecord
from computegrid_api.db.session import SessionFactory, get_session_factory, transaction


class DemoRateLimitExceededError(Exception):
    """The caller exceeded one bounded demo request budget."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Anonymous demo rate limit exceeded")


class DemoProtectionUnavailableError(Exception):
    """Shared abuse protection could not be enforced safely."""


@dataclass(frozen=True, slots=True)
class DemoRateLimit:
    """One fixed-window request budget."""

    scope: str
    limit: int
    window_seconds: int


class DemoAccessService:
    """Apply shared rate limits through atomic PostgreSQL upserts."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def check(self, *, subject: str, budget: DemoRateLimit) -> None:
        now = datetime.now(UTC)
        statement = (
            insert(DemoRateLimitRecord)
            .values(
                scope=budget.scope,
                subject=subject,
                window_started_at=now,
                request_count=1,
            )
            .on_conflict_do_update(
                index_elements=[DemoRateLimitRecord.scope, DemoRateLimitRecord.subject],
                set_={
                    "window_started_at": now,
                    "request_count": 1,
                },
                where=(
                    DemoRateLimitRecord.window_started_at
                    <= now - timedelta(seconds=budget.window_seconds)
                ),
            )
            .returning(DemoRateLimitRecord.request_count)
        )
        final_count: int
        try:
            with transaction(self._session_factory) as session:
                if budget.scope == "global-requests":
                    session.execute(
                        delete(DemoRateLimitRecord).where(
                            DemoRateLimitRecord.window_started_at
                            <= now - timedelta(seconds=budget.window_seconds)
                        )
                    )
                count = session.scalar(statement)
                if count is None:
                    row = session.get(
                        DemoRateLimitRecord,
                        (budget.scope, subject),
                        with_for_update=True,
                    )
                    if row is None:
                        raise RuntimeError("Demo rate-limit row disappeared")
                    elapsed = (now - row.window_started_at).total_seconds()
                    if elapsed >= budget.window_seconds:
                        row.window_started_at = now
                        row.request_count = 1
                        count = 1
                    else:
                        row.request_count += 1
                        count = row.request_count
                final_count = count
        except SQLAlchemyError as error:
            raise DemoProtectionUnavailableError from error
        if final_count > budget.limit:
            raise DemoRateLimitExceededError(budget.window_seconds)


def get_demo_access_service() -> DemoAccessService:
    return DemoAccessService(get_session_factory())
