from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    app_name: str = "DevCockpit"
    environment: str = "development"
    database_url: str = "sqlite+pysqlite:///./devcockpit.db"
    projects_config_path: str = "projects.json"
    github_token: SecretStr | None = None
    github_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    execution_poll_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    dev_stale_after_seconds: float = Field(default=3600.0, ge=300.0, le=86400.0)
    max_parallel_dev_executions: int = Field(default=2, ge=1, le=32)
    resource_lock_lease_seconds: float = Field(default=900.0, ge=30.0, le=86400.0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DEVCOCKPIT_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
