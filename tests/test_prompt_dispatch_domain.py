from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.domain.prompt_dispatch import (
    InvalidPromptDispatchTransition,
    PromptDispatch,
    PromptDispatchError,
    PromptDispatchRole,
    PromptDispatchStatus,
    build_agent_session,
)


def test_prepare_creates_stable_identity_and_logical_session() -> None:
    dispatch_id = UUID("7fcd3422-3dbd-481f-a5b0-5915a1f7f5be")
    now = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)

    dispatch = PromptDispatch.prepare(
        dispatch_id=dispatch_id,
        project_id="DevCockpit",
        work_item_id="DC-010",
        role=PromptDispatchRole.DEV,
        prompt_text="Implement DC-010",
        idempotency_key="dc010:first",
        now=now,
    )

    assert dispatch.dispatch_id == dispatch_id
    assert dispatch.agent_session == "DevCockpit:DEV:DC-010"
    assert dispatch.status is PromptDispatchStatus.PREPARED
    assert dispatch.created_at == now
    assert dispatch.updated_at == now


def test_session_components_and_role_are_validated() -> None:
    with pytest.raises(PromptDispatchError):
        build_agent_session("Dev:Cockpit", PromptDispatchRole.DEV, "DC-010")

    with pytest.raises(PromptDispatchError):
        PromptDispatch.prepare(
            project_id="DevCockpit",
            work_item_id="DC:010",
            role="DEV",
            prompt_text="x",
            idempotency_key="key",
        )

    with pytest.raises(PromptDispatchError):
        PromptDispatch.prepare(
            project_id="DevCockpit",
            work_item_id="DC-010",
            role="UNKNOWN",
            prompt_text="x",
            idempotency_key="key",
        )


def test_only_dc010_transition_is_prepared_to_cancelled() -> None:
    now = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    dispatch = PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-010",
        role="DEV",
        prompt_text="Implement DC-010",
        idempotency_key="dc010:cancel",
        now=now,
    )

    dispatch.cancel(now=now + timedelta(minutes=1))

    assert dispatch.status is PromptDispatchStatus.CANCELLED
    assert dispatch.updated_at == now + timedelta(minutes=1)

    with pytest.raises(InvalidPromptDispatchTransition):
        dispatch.transition_to(PromptDispatchStatus.PREPARED)
