from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.application.prompt_deliveries import (
    AcknowledgementResult,
    acknowledge_prompt_delivery,
    prepare_acknowledged_prompt_redelivery,
    prepare_prompt_deliveries_for_send,
)
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.config import Settings
from app.domain.prompt_delivery import PromptDelivery, PromptDeliveryStatus
from app.domain.prompt_dispatch import PromptDispatch
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_deliveries import PromptDeliveryRecord
from app.infrastructure.prompt_dispatches import PromptDispatchRecord, SqlAlchemyUnitOfWork


def _persistence(tmp_path: Path):
    database_path = tmp_path / "prompt-delivery.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    upgrade_database(settings)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    return settings, engine, session_factory


def test_delivery_domain_records_attempt_and_idempotent_ack() -> None:
    now = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    delivery = PromptDelivery.create(
        dispatch_id=UUID("7fcd3422-3dbd-481f-a5b0-5915a1f7f5be"),
        delivery_id=UUID("8fcd3422-3dbd-481f-a5b0-5915a1f7f5be"),
        now=now,
    )

    delivery.record_attempt(now=now + timedelta(seconds=1))
    changed = delivery.acknowledge(now=now + timedelta(seconds=2))
    duplicate = delivery.acknowledge(now=now + timedelta(seconds=3))

    assert changed is True
    assert duplicate is False
    assert delivery.status is PromptDeliveryStatus.ACKNOWLEDGED
    assert delivery.attempt_count == 1
    assert delivery.acknowledged_at == now + timedelta(seconds=2)


def test_migration_upgrades_from_0001_and_adds_transport_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")

    upgrade_database(settings, "0001_prompt_dispatch")
    engine = build_engine(settings)
    try:
        assert "prompt_dispatches" in inspect(engine).get_table_names()
        assert "prompt_deliveries" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    upgrade_database(settings, "head")
    engine = build_engine(settings)
    try:
        inspector = inspect(engine)
        assert "prompt_deliveries" in inspector.get_table_names()
        uniques = {item["name"] for item in inspector.get_unique_constraints("prompt_deliveries")}
        assert "uq_prompt_deliveries_dispatch_id" in uniques
        foreign_keys = {item["name"] for item in inspector.get_foreign_keys("prompt_deliveries")}
        assert "fk_prompt_deliveries_dispatch_id" in foreign_keys
        checks = {item["name"] for item in inspector.get_check_constraints("prompt_deliveries")}
        assert {
            "ck_prompt_deliveries_status",
            "ck_prompt_deliveries_attempt_count",
            "ck_prompt_deliveries_attempt_timestamp",
            "ck_prompt_deliveries_ack_state",
        } <= checks
    finally:
        engine.dispose()


def test_replay_reuses_same_delivery_and_never_creates_new_dispatch(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id="DC-011",
            role="DEV",
            prompt_text="Implement DC-011",
            idempotency_key="dc011:replay",
        ),
        uow_factory=factory,
    )

    try:
        first = prepare_prompt_deliveries_for_send(uow_factory=factory)
        second = prepare_prompt_deliveries_for_send(uow_factory=factory)

        assert len(first) == 1
        assert len(second) == 1
        assert second[0].delivery_id == first[0].delivery_id
        assert second[0].dispatch_id == dispatch.dispatch_id
        assert first[0].attempt_count == 1
        assert second[0].attempt_count == 2

        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(PromptDispatchRecord)) == 1
            assert session.scalar(select(func.count()).select_from(PromptDeliveryRecord)) == 1
    finally:
        engine.dispose()


def test_ack_is_transport_only_duplicate_safe_and_unknown_is_explicit(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id="DC-011",
            role="DEV",
            prompt_text="Transport prompt",
            idempotency_key="dc011:ack",
        ),
        uow_factory=factory,
    )
    outbound = prepare_prompt_deliveries_for_send(uow_factory=factory)
    delivery_id = outbound[0].delivery_id

    try:
        assert (
            acknowledge_prompt_delivery(delivery_id, uow_factory=factory)
            is AcknowledgementResult.ACKNOWLEDGED
        )
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            acknowledged_at = uow.prompt_deliveries.get(delivery_id).acknowledged_at
            loaded_dispatch = uow.prompt_dispatches.get(dispatch.dispatch_id)
            assert loaded_dispatch.status.value == "PREPARED"

        assert (
            acknowledge_prompt_delivery(delivery_id, uow_factory=factory)
            is AcknowledgementResult.DUPLICATE
        )
        assert (
            acknowledge_prompt_delivery(uuid4(), uow_factory=factory)
            is AcknowledgementResult.UNKNOWN
        )

        with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert uow.prompt_deliveries.get(delivery_id).acknowledged_at == acknowledged_at
    finally:
        engine.dispose()


def test_manual_redelivery_preserves_ack_and_reuses_same_delivery(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id="DC-011",
            role="DEV",
            prompt_text="Manual resend",
            idempotency_key="dc011:manual-resend",
        ),
        uow_factory=factory,
    )
    first = prepare_prompt_deliveries_for_send(uow_factory=factory)
    delivery_id = first[0].delivery_id

    try:
        assert (
            acknowledge_prompt_delivery(delivery_id, uow_factory=factory)
            is AcknowledgementResult.ACKNOWLEDGED
        )
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            original_ack = uow.prompt_deliveries.get(delivery_id).acknowledged_at

        resent = prepare_acknowledged_prompt_redelivery(
            dispatch.dispatch_id,
            uow_factory=factory,
        )

        assert resent.delivery_id == delivery_id
        assert resent.dispatch_id == dispatch.dispatch_id
        assert resent.session == "DevCockpit:DEV:DC-011"
        assert resent.text == "Manual resend"
        assert resent.attempt_count == 2
        assert prepare_prompt_deliveries_for_send(uow_factory=factory) == ()

        with SqlAlchemyUnitOfWork(session_factory) as uow:
            loaded = uow.prompt_deliveries.get(delivery_id)
            assert loaded.is_acknowledged is True
            assert loaded.acknowledged_at == original_ack
            assert loaded.attempt_count == 2
    finally:
        engine.dispose()


def test_cancelled_dispatch_is_not_delivered_or_replayed(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    cancelled = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-011-CANCELLED",
        role="DEV",
        prompt_text="Do not deliver",
        idempotency_key="dc011:cancelled",
    )
    cancelled.cancel()

    try:
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.prompt_dispatches.add(cancelled)
            uow.commit()

        assert prepare_prompt_deliveries_for_send(uow_factory=factory) == ()
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(PromptDeliveryRecord)) == 0
    finally:
        engine.dispose()


def test_multiple_agent_sessions_remain_isolated(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    factory = lambda: SqlAlchemyUnitOfWork(session_factory)

    try:
        for work_item, role in (("DC-011", "DEV"), ("DC-011-ARCH", "ARCH")):
            create_prompt_dispatch(
                CreatePromptDispatchCommand(
                    project_id="DevCockpit",
                    work_item_id=work_item,
                    role=role,
                    prompt_text=f"Prompt for {work_item}",
                    idempotency_key=f"session:{work_item}",
                ),
                uow_factory=factory,
            )

        outbound = prepare_prompt_deliveries_for_send(uow_factory=factory)
        by_session = {item.session: item for item in outbound}

        assert set(by_session) == {
            "DevCockpit:DEV:DC-011",
            "DevCockpit:ARCH:DC-011-ARCH",
        }
        assert len({item.delivery_id for item in outbound}) == 2
    finally:
        engine.dispose()


def test_database_rejects_second_logical_delivery_for_same_dispatch(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    dispatch = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-011",
        role="DEV",
        prompt_text="Unique transport delivery",
        idempotency_key="dc011:unique-delivery",
    )

    try:
        with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.prompt_dispatches.add(dispatch)
            uow.flush()
            first = PromptDelivery.create(dispatch_id=dispatch.dispatch_id)
            first.record_attempt()
            uow.prompt_deliveries.save(first)
            uow.commit()

        with pytest.raises(IntegrityError):
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                second = PromptDelivery.create(dispatch_id=dispatch.dispatch_id)
                second.record_attempt()
                uow.prompt_deliveries.save(second)
                uow.commit()
    finally:
        engine.dispose()


def test_unit_of_work_rolls_back_delivery_and_dispatch_after_flush(tmp_path: Path) -> None:
    _, engine, session_factory = _persistence(tmp_path)
    dispatch = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-011-ROLLBACK",
        role="DEV",
        prompt_text="Rollback transport",
        idempotency_key="dc011:rollback",
    )
    delivery = PromptDelivery.create(dispatch_id=dispatch.dispatch_id)
    delivery.record_attempt()

    try:
        with pytest.raises(RuntimeError):
            with SqlAlchemyUnitOfWork(session_factory) as uow:
                uow.prompt_dispatches.add(dispatch)
                uow.flush()
                uow.prompt_deliveries.save(delivery)
                uow.flush()
                raise RuntimeError("boom")

        with session_factory() as session:
            assert session.get(PromptDispatchRecord, str(dispatch.dispatch_id)) is None
            assert session.get(PromptDeliveryRecord, str(delivery.delivery_id)) is None
    finally:
        engine.dispose()
