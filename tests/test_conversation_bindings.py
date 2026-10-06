from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.application.chatgpt_prompt_sends import (
    ChatGptSendBindingConflict,
    RecordChatGptSendStatusCommand,
    _promote_binding,
)
from app.application.conversation_bindings import (
    BindConversationCommand,
    ConversationBindingInvalidatedError,
    InvalidateConversationBindingCommand,
    bind_conversation,
    conversation_routing_snapshot,
    invalidate_conversation_binding,
)
from app.config import Settings
from app.domain.chatgpt_prompt_send import ChatGptPromptSendState
from app.domain.conversation_binding import (
    ConversationBinding,
    ConversationBindingError,
    ConversationBindingState,
    is_legacy_synthetic_conversation_id,
    normalize_chatgpt_conversation_url,
)
from app.infrastructure.database import build_alembic_config, build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


NOW = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)


def _persistence(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/binding.db")
    upgrade_database(settings)
    engine = build_engine(settings)
    return settings, engine, build_session_factory(engine)


def test_binding_normalizes_supported_chatgpt_url_and_invalidates_explicitly() -> None:
    binding = ConversationBinding.bind(
        agent_session="DevCockpit:DEV:DC-063A",
        conversation_id="abc-123",
        canonical_url="https://chat.openai.com/c/abc-123/?model=auto",
        now=NOW,
    )
    assert binding.canonical_url == "https://chatgpt.com/c/abc-123"
    assert binding.state is ConversationBindingState.BOUND
    assert binding.version == 1

    binding.mark_validated(now=NOW + timedelta(minutes=1))
    assert binding.version == 2
    assert binding.invalidate("conversation_not_reachable", now=NOW + timedelta(minutes=2))
    assert binding.state is ConversationBindingState.INVALIDATED
    assert binding.version == 3
    assert binding.invalidation_reason == "conversation_not_reachable"
    assert binding.invalidate("ignored_duplicate", now=NOW + timedelta(minutes=3)) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://chatgpt.com/",
        "https://chatgpt.com/share/abc",
        "http://chatgpt.com/c/abc",
        "https://example.com/c/abc",
    ],
)
def test_binding_rejects_non_conversation_urls(url: str) -> None:
    with pytest.raises(ConversationBindingError):
        normalize_chatgpt_conversation_url(url)


def test_migration_0008_preserves_existing_data_and_is_reversible(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/from-0007.db")
    upgrade_database(settings, "0007_resource_locks")
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
                    :id, 'DevCockpit', 'DC-063A', 'DEV',
                    'DevCockpit:DEV:DC-063A', 'historical',
                    'PREPARED', 'historical-dc063a', :now, :now
                )
                """
            ),
            {"id": dispatch_id, "now": NOW},
        )
    engine.dispose()

    upgrade_database(settings)
    engine = build_engine(settings)
    inspector = inspect(engine)
    assert "conversation_bindings" in inspector.get_table_names()
    assert "ix_conversation_bindings_state" in {
        index["name"] for index in inspector.get_indexes("conversation_bindings")
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT prompt_text FROM prompt_dispatches WHERE dispatch_id=:id"),
            {"id": dispatch_id},
        ).scalar_one() == "historical"
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010_pr_finalization_attempts"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()

    command.downgrade(build_alembic_config(settings), "0007_resource_locks")
    downgraded_engine = build_engine(settings)
    assert "conversation_bindings" not in inspect(downgraded_engine).get_table_names()
    with downgraded_engine.connect() as connection:
        assert connection.execute(
            text("SELECT prompt_text FROM prompt_dispatches WHERE dispatch_id=:id"),
            {"id": dispatch_id},
        ).scalar_one() == "historical"
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0007_resource_locks"
    downgraded_engine.dispose()


def test_binding_persists_reconstructs_and_routes_after_restart(tmp_path: Path) -> None:
    settings, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)

    binding = bind_conversation(
        BindConversationCommand(
            agent_session="DevCockpit:DEV:DC-063A",
            conversation_id="conv-a",
            canonical_url="https://chatgpt.com/c/conv-a",
        ),
        uow_factory=factory,
        now=NOW,
    )
    assert binding.version == 1
    snapshot = conversation_routing_snapshot(
        "DevCockpit:DEV:DC-063A",
        uow_factory=factory,
    )
    assert snapshot is not None
    assert snapshot.binding_version == 1
    assert snapshot.conversation_id == "conv-a"
    engine.dispose()

    restarted_engine = build_engine(settings)
    restarted_factory = build_session_factory(restarted_engine)
    restarted_uow = lambda: SqlAlchemyUnitOfWork(restarted_factory)
    snapshot = conversation_routing_snapshot(
        "DevCockpit:DEV:DC-063A",
        uow_factory=restarted_uow,
    )
    assert snapshot is not None
    assert snapshot.canonical_url == "https://chatgpt.com/c/conv-a"

    invalidated = invalidate_conversation_binding(
        InvalidateConversationBindingCommand(
            agent_session="DevCockpit:DEV:DC-063A",
            reason="exact_target_no_longer_safe",
        ),
        uow_factory=restarted_uow,
        now=NOW + timedelta(minutes=1),
    )
    assert invalidated is not None
    assert invalidated.state is ConversationBindingState.INVALIDATED
    with pytest.raises(ConversationBindingInvalidatedError):
        conversation_routing_snapshot(
            "DevCockpit:DEV:DC-063A",
            uow_factory=restarted_uow,
        )
    restarted_engine.dispose()


def test_database_enforces_unique_session_and_conversation(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    try:
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            first = ConversationBinding.bind(
                agent_session="DevCockpit:DEV:A",
                conversation_id="same-conversation",
                canonical_url="https://chatgpt.com/c/same-conversation",
                now=NOW,
            )
            uow.conversation_bindings.save(first)
            uow.commit()

        with pytest.raises(IntegrityError):
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                second = ConversationBinding.bind(
                    agent_session="DevCockpit:DEV:B",
                    conversation_id="same-conversation",
                    canonical_url="https://chatgpt.com/c/same-conversation",
                    now=NOW,
                )
                uow.conversation_bindings.save(second)
                uow.commit()
    finally:
        engine.dispose()


def test_legacy_synthetic_conversation_id_recognizes_encoded_and_plain_forms() -> None:
    assert is_legacy_synthetic_conversation_id("local-chatgpt:abc")
    assert is_legacy_synthetic_conversation_id("local-chatgpt%3Aabc")
    assert is_legacy_synthetic_conversation_id("LOCAL-CHATGPT%3aABC")
    assert not is_legacy_synthetic_conversation_id("real-conversation")


def test_confirmed_real_send_rebinds_legacy_synthetic_binding(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    bind_conversation(
        BindConversationCommand(
            agent_session="DevCockpit:DEV:DC-LEGACY",
            conversation_id="local-chatgpt%3Alegacy-id",
            canonical_url="https://chatgpt.com/c/local-chatgpt%3Alegacy-id",
        ),
        uow_factory=factory,
        now=NOW,
    )
    command = RecordChatGptSendStatusCommand(
        event_id=uuid4(),
        delivery_id=uuid4(),
        session="DevCockpit:DEV:DC-LEGACY",
        state=ChatGptPromptSendState.SENT_CONFIRMED,
        attempt_count=1,
        conversation_id="real-conversation",
        canonical_url="https://chatgpt.com/c/real-conversation",
        error_code=None,
        next_retry_at=None,
        occurred_at=NOW,
    )

    with factory() as uow:
        _promote_binding(command, uow=uow, recorded_at=NOW + timedelta(minutes=1))
        uow.commit()

    snapshot = conversation_routing_snapshot(
        "DevCockpit:DEV:DC-LEGACY",
        uow_factory=factory,
    )
    assert snapshot is not None
    assert snapshot.conversation_id == "real-conversation"
    assert snapshot.canonical_url == "https://chatgpt.com/c/real-conversation"
    assert snapshot.binding_version == 2
    engine.dispose()


def test_synthetic_confirmed_send_is_never_promoted(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    command = RecordChatGptSendStatusCommand(
        event_id=uuid4(),
        delivery_id=uuid4(),
        session="DevCockpit:DEV:DC-LEGACY",
        state=ChatGptPromptSendState.SENT_CONFIRMED,
        attempt_count=1,
        conversation_id="local-chatgpt%3Asynthetic",
        canonical_url="https://chatgpt.com/c/local-chatgpt%3Asynthetic",
        error_code=None,
        next_retry_at=None,
        occurred_at=NOW,
    )

    with factory() as uow:
        with pytest.raises(ChatGptSendBindingConflict):
            _promote_binding(command, uow=uow, recorded_at=NOW)
    engine.dispose()
