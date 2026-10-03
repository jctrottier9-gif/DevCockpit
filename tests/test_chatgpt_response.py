from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.application.chatgpt_responses import (
    ImportChatGptResponseCommand,
    ResponseEchoesPromptError,
    ResponseIdConflictError,
    ResponseImportResult,
    ResponseSessionMismatchError,
    UnknownPromptDeliveryError,
    import_chatgpt_response,
    list_imported_chatgpt_responses,
)
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.config import Settings
from app.domain.prompt_dispatch import PromptDispatchStatus
from app.infrastructure.chatgpt_responses import ImportedChatGptResponseRecord
from app.infrastructure.database import (
    build_alembic_config,
    build_engine,
    build_session_factory,
    upgrade_database,
)
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


RESPONSE_ID = UUID("10cd3422-3dbd-481f-a5b0-5915a1f7f5be")


def _persistence(tmp_path: Path):
    database_path = tmp_path / "response.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    upgrade_database(settings)
    engine = build_engine(settings)
    return settings, engine, build_session_factory(engine)


def _source_delivery(session_factory):
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id="DC-030",
            role="DEV",
            prompt_text="Implement response return",
            idempotency_key="dc030:source",
        ),
        uow_factory=factory,
    )
    delivery = prepare_prompt_deliveries_for_send(uow_factory=factory)[0]
    return factory, dispatch, delivery


def test_migration_from_0002_is_additive_and_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    upgrade_database(settings, "0002_prompt_delivery")
    upgrade_database(settings, "head")
    engine = build_engine(settings)
    try:
        inspector = inspect(engine)
        assert "imported_chatgpt_responses" in inspector.get_table_names()
        assert "fk_imported_chatgpt_responses_delivery_id" in {
            item["name"] for item in inspector.get_foreign_keys("imported_chatgpt_responses")
        }
        assert "ck_imported_chatgpt_responses_text" in {
            item["name"] for item in inspector.get_check_constraints("imported_chatgpt_responses")
        }
    finally:
        engine.dispose()

    command.downgrade(build_alembic_config(settings), "0002_prompt_delivery")
    engine = build_engine(settings)
    try:
        assert "imported_chatgpt_responses" not in inspect(engine).get_table_names()
        assert {"prompt_dispatches", "prompt_deliveries"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_import_round_trip_and_identical_replay_are_idempotent(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory, dispatch, delivery = _source_delivery(session_factory)
    text = "Paragraph one.\n\n- item\n\n```python\nprint('ok')\n```"
    value = ImportChatGptResponseCommand(
        response_id=RESPONSE_ID,
        delivery_id=delivery.delivery_id,
        session=dispatch.agent_session,
        text=text,
    )
    try:
        assert import_chatgpt_response(value, uow_factory=factory) is ResponseImportResult.IMPORTED
        assert import_chatgpt_response(value, uow_factory=factory) is ResponseImportResult.DUPLICATE
        views = list_imported_chatgpt_responses("DevCockpit", uow_factory=factory)
        assert len(views) == 1
        assert views[0].delivery_id == delivery.delivery_id
        assert views[0].session == "DevCockpit:DEV:DC-030"
        assert views[0].work_item_id == "DC-030"
        assert views[0].role == "DEV"
        assert views[0].text == text
        assert views[0].imported_at.tzinfo is not None
    finally:
        engine.dispose()


def test_prompt_echo_is_rejected_before_persistence(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory, dispatch, delivery = _source_delivery(session_factory)
    try:
        echoed_values = (
            dispatch.prompt_text,
            "  " + dispatch.prompt_text.replace(" ", "\n") + "  ",
            "**Implement** response return",
        )
        for echoed in echoed_values:
            with pytest.raises(ResponseEchoesPromptError):
                import_chatgpt_response(
                    ImportChatGptResponseCommand(
                        uuid4(),
                        delivery.delivery_id,
                        dispatch.agent_session,
                        echoed,
                    ),
                    uow_factory=factory,
                )
        with session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(ImportedChatGptResponseRecord)
            ) == 0
    finally:
        engine.dispose()

def test_response_id_collision_is_explicit_and_never_overwrites(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory, dispatch, delivery = _source_delivery(session_factory)
    try:
        import_chatgpt_response(
            ImportChatGptResponseCommand(RESPONSE_ID, delivery.delivery_id, dispatch.agent_session, "first"),
            uow_factory=factory,
        )
        with pytest.raises(ResponseIdConflictError):
            import_chatgpt_response(
                ImportChatGptResponseCommand(RESPONSE_ID, delivery.delivery_id, dispatch.agent_session, "changed"),
                uow_factory=factory,
            )
        with session_factory() as session:
            row = session.get(ImportedChatGptResponseRecord, str(RESPONSE_ID))
            assert row.text == "first"
    finally:
        engine.dispose()


def test_unknown_delivery_and_session_mismatch_persist_nothing(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory, dispatch, delivery = _source_delivery(session_factory)
    try:
        with pytest.raises(UnknownPromptDeliveryError):
            import_chatgpt_response(
                ImportChatGptResponseCommand(RESPONSE_ID, uuid4(), dispatch.agent_session, "orphan"),
                uow_factory=factory,
            )
        with pytest.raises(ResponseSessionMismatchError):
            import_chatgpt_response(
                ImportChatGptResponseCommand(RESPONSE_ID, delivery.delivery_id, "DevCockpit:ARCH:DC-030", "wrong"),
                uow_factory=factory,
            )
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ImportedChatGptResponseRecord)) == 0
    finally:
        engine.dispose()


def test_multiple_responses_same_delivery_and_cancelled_prompt_are_historical(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory, dispatch, delivery = _source_delivery(session_factory)
    try:
        with factory() as uow:
            loaded = uow.prompt_dispatches.get(dispatch.dispatch_id)
            assert loaded is not None
            loaded.cancel(now=datetime.now(timezone.utc))
            uow.prompt_dispatches.save(loaded)
            uow.commit()

        for response_id, text in ((RESPONSE_ID, "first"), (uuid4(), "second")):
            import_chatgpt_response(
                ImportChatGptResponseCommand(
                    response_id,
                    delivery.delivery_id,
                    dispatch.agent_session,
                    text,
                ),
                uow_factory=factory,
            )
        assert len(list_imported_chatgpt_responses("DevCockpit", uow_factory=factory)) == 2
        with factory() as uow:
            assert uow.prompt_dispatches.get(dispatch.dispatch_id).status is PromptDispatchStatus.CANCELLED
    finally:
        engine.dispose()


def test_database_enforces_delivery_fk(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    try:
        with pytest.raises(IntegrityError):
            with session_factory() as session:
                session.add(
                    ImportedChatGptResponseRecord(
                        response_id=str(RESPONSE_ID),
                        delivery_id=str(uuid4()),
                        text="orphan",
                        imported_at=datetime.now(timezone.utc),
                    )
                )
                session.commit()
    finally:
        engine.dispose()
