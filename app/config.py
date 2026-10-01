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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DEVCOCKPIT_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
