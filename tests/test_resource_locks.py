from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from sqlalchemy import inspect, text

from app.config import Settings
from app.domain.resource_lock import (
    ResourceLockMode,
    ResourceLockRequirement,
    ResourceLockState,
)
from app.infrastructure.database import (
    build_engine,
    build_session_factory,
    upgrade_database,
)
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def test_upgrade_from_0006_preserves_existing_data_and_persists_resource_locks(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/from-0006.db")
    upgrade_database(settings, "0006_roadmap_writeback")
    engine = build_engine(settings)
    dispatch_id = str(uuid4())

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prompt_dispatches (
                    dispatch_id, project_id, work_item_id, role, agent_session,
                    prompt_text, status, idempotency_key, created_at, updated_at
                ) VALUES (
                    :id, 'DevCockpit', 'DC-051', 'DEV',
                    'DevCockpit:DEV:DC-051', 'historical',
                    'PREPARED', 'historical-key', :now, :now
                )
                """
            ),
            {"id": dispatch_id, "now": NOW},
        )
    engine.dispose()

    upgrade_database(settings)
    engine = build_engine(settings)
    inspector = inspect(engine)
    assert "resource_locks" in inspector.get_table_names()
    assert {
        "ix_resource_locks_project_state_surface",
        "ix_resource_locks_owner_state",
    } <= {index["name"] for index in inspector.get_indexes("resource_locks")}

    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT prompt_text FROM prompt_dispatches "
                "WHERE dispatch_id=:dispatch_id"
            ),
            {"dispatch_id": dispatch_id},
        ).scalar_one() == "historical"
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0008_conversation_binding"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    session_factory = build_session_factory(engine)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        acquired, conflict = uow.resource_locks.acquire_many(
            project_id="DevCockpit",
            work_item_id="DC-052",
            agent_session="DevCockpit:DEV:DC-052",
            lease_owner_id="process-one",
            requirements=(
                ResourceLockRequirement.build(
                    "migration:alembic",
                    ResourceLockMode.EXCLUSIVE,
                ),
            ),
            now=NOW,
            lease_seconds=900,
        )
        assert conflict is None
        assert len(acquired) == 1
        uow.commit()
    engine.dispose()

    restarted_engine = build_engine(settings)
    restarted_factory = build_session_factory(restarted_engine)
    with SqlAlchemyUnitOfWork(restarted_factory) as uow:
        locks = uow.resource_locks.list_for_owner("DevCockpit", "DC-052")
        assert len(locks) == 1
        assert locks[0].surface.key == "migration:alembic"
        assert locks[0].state is ResourceLockState.ACTIVE
        assert locks[0].lease_owner_id == "process-one"
    restarted_engine.dispose()


def test_real_sqlite_concurrency_never_grants_same_exclusive_surface_twice(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/concurrent.db")
    upgrade_database(settings)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    start = Barrier(2)
    requirement = (
        ResourceLockRequirement.build(
            "file:critical.py",
            ResourceLockMode.EXCLUSIVE,
        ),
    )

    def attempt(work_item_id: str) -> tuple[str, bool]:
        start.wait()
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            acquired, conflict = uow.resource_locks.acquire_many(
                project_id="DevCockpit",
                work_item_id=work_item_id,
                agent_session=f"DevCockpit:DEV:{work_item_id}",
                lease_owner_id=f"process-{work_item_id}",
                requirements=requirement,
                now=NOW,
                lease_seconds=900,
            )
            uow.commit()
            return work_item_id, bool(acquired) and conflict is None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("A", "B")))

    assert sum(1 for _, acquired in results if acquired) == 1

    with SqlAlchemyUnitOfWork(session_factory) as uow:
        active = [
            lock
            for lock in uow.resource_locks.list_active_for_project("DevCockpit")
            if lock.surface.key == "file:critical.py"
        ]
        assert len(active) == 1
        assert active[0].work_item_id in {"A", "B"}
    engine.dispose()
