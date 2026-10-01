from pathlib import Path

from sqlalchemy import text

from app.config import Settings
from app.infrastructure.database import (
    build_engine,
    build_session_factory,
    initialize_database,
)


def test_sqlite_bootstrap_is_reproducible(tmp_path: Path) -> None:
    database_path = tmp_path / "devcockpit-test.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = build_engine(settings)

    try:
        initialize_database(engine)
        session_factory = build_session_factory(engine)
        with session_factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()

    assert database_path.exists()
