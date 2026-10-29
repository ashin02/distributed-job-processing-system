"""SQLAlchemy models for durable job state and outbox delivery."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class JobState(StrEnum):
    """Durable states in the job lifecycle."""

    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    """Durable states for one physical job attempt."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


job_state_enum = Enum(JobState, name="job_state")
attempt_state_enum = Enum(AttemptState, name="attempt_state")


class Base(DeclarativeBase):
    """Base class for all ComputeGrid database models."""


class TimestampMixin:
    """Database-managed creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Job(TimestampMixin, Base):
    """Authoritative state and externally visible result for a logical job."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_jobs_client_idempotency_key"),
        CheckConstraint("char_length(request_hash) = 64", name="ck_jobs_request_hash_length"),
        CheckConstraint("version >= 0", name="ck_jobs_version_nonnegative"),
        CheckConstraint(
            "state != 'SUCCEEDED' OR (result IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_jobs_succeeded_has_result",
        ),
        Index("ix_jobs_state_created_at", "state", "created_at"),
        Index("ix_jobs_client_created_at", "client_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[JobState] = mapped_column(
        job_state_enum, nullable=False, default=JobState.ACCEPTED, server_default="ACCEPTED"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list[JobAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobAttempt.attempt_number"
    )
    outbox_events: Mapped[list[OutboxEvent]] = relationship(
        back_populates="job", passive_deletes=True
    )


class JobAttempt(TimestampMixin, Base):
    """One claim and execution attempt for a job."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        UniqueConstraint("job_id", "fencing_token", name="uq_job_attempts_job_fencing_token"),
        CheckConstraint("attempt_number > 0", name="ck_job_attempts_number_positive"),
        CheckConstraint("fencing_token > 0", name="ck_job_attempts_fencing_token_positive"),
        Index("ix_job_attempts_state_lease", "state", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[AttemptState] = mapped_column(
        attempt_state_enum, nullable=False, default=AttemptState.PENDING, server_default="PENDING"
    )
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    job: Mapped[Job] = relationship(back_populates="attempts")


class OutboxEvent(Base):
    """Event committed atomically with job state for later publication."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("publish_attempts >= 0", name="ck_outbox_publish_attempts_nonnegative"),
        Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="outbox_events")
