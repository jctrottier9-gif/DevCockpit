from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    IdempotencyConflictError,
    create_prompt_dispatch,
)
from app.config import Settings
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyPromptDispatchRepository, SqlAlchemyUnitOfWork


def _persistence(tmp_path: Path):
    database_path = tmp_path / "prompt-dispatch.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    upgrade_database(settings)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    return engine, session_factory


def test_migration_creates_expected_schema_and_constraints(tmp_path: Path) -> None:
    engine, _ = _persistence(tmp_path)
    try:
        inspector = inspect(engine)
        assert "prompt_dispatches" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("prompt_dispatches")}
        assert columns == {
            "dispatch_id",
            "project_id",
            "work_item_id",
            "role",
            "agent_session",
            "prompt_text",
            "status",
            "idempotency_key",
            "created_at",
            "updated_at",
        }
        uniques = {item["name"] for item in inspector.get_unique_constraints("prompt_dispatches")}
        assert "uq_prompt_dispatches_idempotency_key" in uniques
        checks = {item["name"] for item in inspector.get_check_constraints("prompt_dispatches")}
        assert {"ck_prompt_dispatches_role", "ck_prompt_dispatches_status"} <= checks
    finally:
        engine.dispose()


def test_repository_round_trip_preserves_identity_text_and_timestamps(tmp_path: Path) -> None:
    engine, session_factory = _persistence(tmp_path)
    dispatch = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role=PromptDispatchRole.DEV,
        prompt_text="Full prompt\nwith multiple lines.",
        idempotency_key="round-trip",
    )

    try:
        with session_factory() as session:
            repository = SqlAlchemyPromptDispatchRepository(session)
            repository.add(dispatch)
            session.commit()

        with session_factory() as session:
            loaded = SqlAlchemyPromptDispatchRepository(session).get(dispatch.dispatch_id)

        assert loaded is not None
        assert loaded.dispatch_id == dispatch.dispatch_id
        assert loaded.prompt_text == dispatch.prompt_text
        assert loaded.agent_session == "DevCockpit:DEV:DC-010"
        assert loaded.created_at == dispatch.created_at
        assert loaded.updated_at == dispatch.updated_at
    finally:
        engine.dispose()


def test_database_rejects_duplicate_idempotency_key(tmp_path: Path) -> None:
    engine, session_factory = _persistence(tmp_path)
    first = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role="DEV",
        prompt_text="first",
        idempotency_key="same-key",
    )
    second = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role="DEV",
        prompt_text="second",
        idempotency_key="same-key",
    )
    try:
        with session_factory() as session:
            repository = SqlAlchemyPromptDispatchRepository(session)
            repository.add(first)
            session.commit()
            repository.add(second)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
    finally:
        engine.dispose()


def test_unit_of_work_rolls_back_when_operation_fails(tmp_path: Path) -> None:
    engine, session_factory = _persistence(tmp_path)
    dispatch = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role="DEV",
        prompt_text="rollback",
        idempotency_key="rollback-key",
    )
    try:
        with pytest.raises(RuntimeError):
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                uow.prompt_dispatches.add(dispatch)
                raise RuntimeError("boom")

        with session_factory() as session:
            assert SqlAlchemyPromptDispatchRepository(session).get(dispatch.dispatch_id) is None
    finally:
        engine.dispose()


def test_application_replay_returns_same_dispatch_and_conflict_is_explicit(tmp_path: Path) -> None:
    engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    command = CreatePromptDispatchCommand(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role="DEV",
        prompt_text="Prepare prompt",
        idempotency_key="command-42",
    )
    try:
        first = create_prompt_dispatch(command, uow_factory=factory)
        replay = create_prompt_dispatch(command, uow_factory=factory)

        assert replay.dispatch_id == first.dispatch_id

        with pytest.raises(IdempotencyConflictError):
            create_prompt_dispatch(
                CreatePromptDispatchCommand(
                    project_id="DevCockpit",
                    work_item_id="DC-010",
                    role="DEV",
                    prompt_text="Different prompt",
                    idempotency_key="command-42",
                ),
                uow_factory=factory,
            )

        with session_factory() as session:
            repository = SqlAlchemyPromptDispatchRepository(session)
            assert repository.get_by_idempotency_key("command-42").dispatch_id == first.dispatch_id
    finally:
        engine.dispose()
