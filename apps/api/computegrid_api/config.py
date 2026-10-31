"""Environment-based application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database settings shared by the application and migration tooling."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    database_url: str = Field(min_length=1)
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)


class Settings(DatabaseSettings):
    """Complete API runtime settings."""

    development_api_token: SecretStr
    development_client_id: str = Field(default="local-development", min_length=1, max_length=255)
    demo_mode_enabled: bool = False
    demo_session_secret: SecretStr = SecretStr("")
    demo_session_ttl_seconds: int = Field(default=604_800, ge=300, le=2_592_000)
    demo_cookie_secure: bool = False
    demo_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    demo_submission_limit: int = Field(default=10, ge=1, le=1000)
    demo_read_limit: int = Field(default=120, ge=1, le=10_000)
    demo_operations_limit: int = Field(default=30, ge=1, le=1000)
    demo_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    demo_global_request_limit: int = Field(default=1000, ge=1, le=100_000)
    demo_max_active_jobs_per_client: int = Field(default=3, ge=1, le=100)
    demo_max_active_jobs_global: int = Field(default=100, ge=1, le=10_000)
    demo_max_retained_jobs_per_client: int = Field(default=50, ge=1, le=10_000)
    demo_max_retained_jobs_global: int = Field(default=5000, ge=1, le=1_000_000)
    demo_max_monte_carlo_samples: int = Field(default=1_000_000, ge=1, le=10_000_000)
    demo_max_model_variables: int = Field(default=12, ge=1, le=16)

    @model_validator(mode="after")
    def validate_demo_security(self) -> "Settings":
        if self.demo_mode_enabled and len(self.demo_session_secret.get_secret_value()) < 32:
            raise ValueError(
                "COMPUTEGRID_DEMO_SESSION_SECRET must contain at least 32 characters "
                "when anonymous demo mode is enabled"
            )
        if self.demo_cookie_samesite == "none" and not self.demo_cookie_secure:
            raise ValueError(
                "SameSite=None demo cookies require COMPUTEGRID_DEMO_COOKIE_SECURE=true"
            )
        if self.demo_max_retained_jobs_per_client < self.demo_max_active_jobs_per_client:
            raise ValueError("Per-client retained-job capacity must cover active-job capacity")
        if self.demo_max_retained_jobs_global < self.demo_max_active_jobs_global:
            raise ValueError("Global retained-job capacity must cover active-job capacity")
        return self


class BrokerSettings(BaseSettings):
    """RabbitMQ transport and outbox relay settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    broker_url: str = Field(default="amqp://guest:guest@localhost:5672//", min_length=1)
    job_queue: str = Field(default="computegrid.jobs", min_length=1)
    outbox_batch_size: int = Field(default=50, ge=1, le=1000)
    outbox_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)


class ComputeDemoSettings(BaseSettings):
    """Server-only computational feed policy; ordinary submissions are unchanged."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    compute_demo_interval_seconds: float = Field(default=0.5, ge=0.1, le=20)
    compute_demo_max_jobs: int = Field(default=40, ge=1, le=40)
    compute_demo_max_feeds: int = Field(default=1, ge=1, le=4)
    compute_demo_active_capacity: int = Field(default=40, ge=1, le=100)
    compute_demo_retained_capacity: int = Field(default=5000, ge=1, le=10000)
    compute_demo_cooldown_seconds: int = Field(default=120, ge=20, le=86400)

    compute_demo_samples: int = Field(
        default=10_000_000,
        ge=1,
        le=10_000_000,
    )

    compute_demo_variables: int = Field(
        default=18,
        ge=4,
        le=18,
    )


@lru_cache
def get_compute_demo_settings() -> ComputeDemoSettings:
    return ComputeDemoSettings()


class RetrySettings(BaseSettings):
    """Bounded durable retry policy for worker execution failures."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    job_max_attempts: int = Field(default=3, ge=1, le=100)
    retry_base_delay_seconds: float = Field(default=1.0, gt=0, le=3600)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0, le=86400)
    retry_jitter_ratio: float = Field(default=0.5, ge=0, le=1)


class LeaseSettings(BaseSettings):
    """Durable worker lease, recovery, and shutdown settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    job_lease_seconds: float = Field(default=300.0, gt=0, le=86400)
    lease_recovery_batch_size: int = Field(default=50, ge=1, le=1000)
    worker_soft_shutdown_timeout_seconds: float = Field(default=30.0, ge=0, le=3600)


class OperationsSettings(BaseSettings):
    """Bounded dependency access used by the read-only operations endpoint."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    rabbitmq_management_url: str = Field(default="http://localhost:15672", min_length=1)
    operations_timeout_seconds: float = Field(default=2.0, gt=0, le=10)


class CorsSettings(BaseSettings):
    """Browser origins allowed during local frontend development."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """Return settings needed by database-only processes."""
    return DatabaseSettings()  # type: ignore[call-arg]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]


@lru_cache
def get_broker_settings() -> BrokerSettings:
    """Return broker settings shared by the relay and Celery worker."""
    return BrokerSettings()


@lru_cache
def get_retry_settings() -> RetrySettings:
    """Return the process-wide durable execution retry policy."""
    return RetrySettings()


@lru_cache
def get_lease_settings() -> LeaseSettings:
    """Return worker lease and recovery settings."""
    return LeaseSettings()


@lru_cache
def get_operations_settings() -> OperationsSettings:
    """Return settings for bounded operational dependency queries."""
    return OperationsSettings()


@lru_cache
def get_cors_settings() -> CorsSettings:
    """Return local browser-origin settings without requiring database credentials."""
    return CorsSettings()
