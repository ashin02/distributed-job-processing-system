"""Environment-based application settings."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database settings shared by the application and migration tooling."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTEGRID_",
        extra="ignore",
    )

    database_url: str = Field(min_length=1)


class Settings(DatabaseSettings):
    """Complete API runtime settings."""

    development_api_token: SecretStr
    development_client_id: str = Field(default="local-development", min_length=1, max_length=255)


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
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)


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
def get_cors_settings() -> CorsSettings:
    """Return local browser-origin settings without requiring database credentials."""
    return CorsSettings()
