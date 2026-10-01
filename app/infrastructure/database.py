from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models introduced by later roadmap slices."""


def build_engine(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    connect_args: dict[str, object] = {}
    if active_settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    return create_engine(active_settings.database_url, connect_args=connect_args)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def initialize_database(engine: Engine | None = None) -> Engine:
    """Create the current metadata and verify that the configured database is reachable."""

    active_engine = engine or build_engine()
    Base.metadata.create_all(bind=active_engine)
    with active_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return active_engine


def main() -> None:
    engine = initialize_database()
    engine.dispose()
    print("DevCockpit database initialized.")


if __name__ == "__main__":
    main()
