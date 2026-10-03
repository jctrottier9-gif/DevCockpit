from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol, Self

from app.domain.conversation_binding import (
    ConversationBinding,
    ConversationBindingState,
)


class ConversationBindingRepository(Protocol):
    def get_by_agent_session(self, agent_session: str) -> ConversationBinding | None: ...
    def save(self, binding: ConversationBinding) -> None: ...


class ConversationBindingUnitOfWork(Protocol):
    conversation_bindings: ConversationBindingRepository

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], ConversationBindingUnitOfWork]


class ConversationBindingInvalidatedError(ValueError):
    def __init__(self, agent_session: str, reason: str | None) -> None:
        super().__init__(f"ConversationBinding invalidated for {agent_session}: {reason or 'unknown'}")
        self.agent_session = agent_session
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BindConversationCommand:
    agent_session: str
    conversation_id: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class InvalidateConversationBindingCommand:
    agent_session: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConversationRoutingSnapshot:
    binding_version: int
    conversation_id: str
    canonical_url: str


def bind_conversation(
    command: BindConversationCommand,
    *,
    uow_factory: UnitOfWorkFactory,
    now: datetime | None = None,
) -> ConversationBinding:
    with uow_factory() as uow:
        binding = uow.conversation_bindings.get_by_agent_session(command.agent_session)
        if binding is None:
            binding = ConversationBinding.bind(
                agent_session=command.agent_session,
                conversation_id=command.conversation_id,
                canonical_url=command.canonical_url,
                now=now,
            )
        elif (
            binding.state is ConversationBindingState.BOUND
            and binding.conversation_id == command.conversation_id
            and binding.canonical_url == command.canonical_url
        ):
            return binding
        else:
            binding.rebind(
                conversation_id=command.conversation_id,
                canonical_url=command.canonical_url,
                now=now,
            )
        uow.conversation_bindings.save(binding)
        uow.commit()
        return binding


def invalidate_conversation_binding(
    command: InvalidateConversationBindingCommand,
    *,
    uow_factory: UnitOfWorkFactory,
    now: datetime | None = None,
) -> ConversationBinding | None:
    with uow_factory() as uow:
        binding = uow.conversation_bindings.get_by_agent_session(command.agent_session)
        if binding is None:
            return None
        changed = binding.invalidate(command.reason, now=now)
        if changed:
            uow.conversation_bindings.save(binding)
            uow.commit()
        return binding


def conversation_routing_snapshot(
    agent_session: str,
    *,
    uow_factory: UnitOfWorkFactory,
) -> ConversationRoutingSnapshot | None:
    with uow_factory() as uow:
        binding = uow.conversation_bindings.get_by_agent_session(agent_session)
        if binding is None:
            return None
        if binding.state is ConversationBindingState.INVALIDATED:
            raise ConversationBindingInvalidatedError(
                agent_session,
                binding.invalidation_reason,
            )
        return ConversationRoutingSnapshot(
            binding_version=binding.version,
            conversation_id=binding.conversation_id,
            canonical_url=binding.canonical_url,
        )
