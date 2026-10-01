from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for persisted models; Alembic is schema authority."""


def build_engine(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    connect_args: dict[str, object] = {}
    if active_settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(active_settings.database_url, connect_args=connect_args)
    if active_settings.database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def initialize_database(engine: Engine | None = None) -> Engine:
    """Verify database connectivity without mutating the business schema."""

    active_engine = engine or build_engine()
    with active_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return active_engine


def build_alembic_config(settings: Settings | None = None) -> Config:
    active_settings = settings or get_settings()
    repository_root = Path(__file__).resolve().parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    config.set_main_option("sqlalchemy.url", active_settings.database_url)
    return config


def upgrade_database(settings: Settings | None = None, revision: str = "head") -> None:
    """Apply explicit Alembic migrations to the configured database."""

    command.upgrade(build_alembic_config(settings), revision)


def main() -> None:
    settings = get_settings()
    upgrade_database(settings)
    engine = initialize_database(build_engine(settings))
    engine.dispose()
    print("DevCockpit database migrated and verified.")


if __name__ == "__main__":
    main()
