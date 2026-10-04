from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Protocol, Self
from uuid import UUID

from app.domain.chatgpt_prompt_send import (
    ChatGptPromptSend,
    ChatGptPromptSendError,
    ChatGptPromptSendState,
    ChatGptSendStatusEvent,
)
from app.domain.conversation_binding import (
    ConversationBindingError,
    ConversationBindingState,
)


class ChatGptPromptSendRepository(Protocol):
    def get(self, delivery_id: object) -> ChatGptPromptSend | None: ...
    def save(self, prompt_send: ChatGptPromptSend) -> None: ...
    def has_event(self, event_id: object) -> bool: ...
    def add_event(self, event: ChatGptSendStatusEvent) -> None: ...


class ChatGptPromptSendUnitOfWork(Protocol):
    prompt_deliveries: object
    prompt_dispatches: object
    chatgpt_prompt_sends: ChatGptPromptSendRepository
    conversation_bindings: object

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], ChatGptPromptSendUnitOfWork]


class ChatGptSendStatusResult(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"


class ChatGptSendStatusError(ValueError):
    code = "CHATGPT_SEND_STATUS_REJECTED"


class UnknownChatGptSendDelivery(ChatGptSendStatusError):
    code = "UNKNOWN_CHATGPT_SEND_DELIVERY"


class ChatGptSendSessionMismatch(ChatGptSendStatusError):
    code = "CHATGPT_SEND_SESSION_MISMATCH"


class ChatGptSendBindingConflict(ChatGptSendStatusError):
    code = "CHATGPT_SEND_BINDING_CONFLICT"


@dataclass(frozen=True, slots=True)
class RecordChatGptSendStatusCommand:
    event_id: UUID
    delivery_id: UUID
    session: str
    state: ChatGptPromptSendState
    attempt_count: int
    conversation_id: str | None
    canonical_url: str | None
    error_code: str | None
    next_retry_at: datetime | None
    occurred_at: datetime


def ensure_chatgpt_prompt_send_for_ack(
    delivery_id: UUID,
    *,
    uow,
    now: datetime | None = None,
) -> ChatGptPromptSend | None:
    """Create the backend QUEUED projection inside the PromptDelivery ACK transaction."""

    repository = getattr(uow, "chatgpt_prompt_sends", None)
    if repository is None:
        return None
    existing = repository.get(delivery_id)
    if existing is not None:
        return existing
    delivery = uow.prompt_deliveries.get(delivery_id)
    if delivery is None:
        return None
    dispatch = uow.prompt_dispatches.get(delivery.dispatch_id)
    if dispatch is None:
        return None
    projection = ChatGptPromptSend.queued(
        delivery_id=delivery_id,
        session=dispatch.agent_session,
        now=now or datetime.now(timezone.utc),
    )
    repository.save(projection)
    return projection


def _promote_binding(
    command: RecordChatGptSendStatusCommand,
    *,
    uow,
    recorded_at: datetime,
) -> None:
    if (
        command.state is not ChatGptPromptSendState.SENT_CONFIRMED
        or command.conversation_id is None
        or command.canonical_url is None
    ):
        return

    current = uow.conversation_bindings.get_by_agent_session(command.session)
    if current is None:
        from app.domain.conversation_binding import ConversationBinding

        uow.conversation_bindings.save(
            ConversationBinding.bind(
                agent_session=command.session,
                conversation_id=command.conversation_id,
                canonical_url=command.canonical_url,
                now=recorded_at,
            )
        )
        return

    if current.state is ConversationBindingState.INVALIDATED:
        raise ChatGptSendBindingConflict(
            f"Cannot confirm send against invalidated binding for {command.session}"
        )
    if (
        current.conversation_id != command.conversation_id
        or current.canonical_url != command.canonical_url
    ):
        raise ChatGptSendBindingConflict(
            f"Confirmed conversation differs from durable binding for {command.session}"
        )


def record_chatgpt_send_status(
    command: RecordChatGptSendStatusCommand,
    *,
    uow_factory: UnitOfWorkFactory,
) -> ChatGptSendStatusResult:
    try:
        event = ChatGptSendStatusEvent(
            event_id=command.event_id,
            delivery_id=command.delivery_id,
            session=command.session,
            state=command.state,
            attempt_count=command.attempt_count,
            conversation_id=command.conversation_id,
            canonical_url=command.canonical_url,
            error_code=command.error_code,
            next_retry_at=command.next_retry_at,
            occurred_at=command.occurred_at,
        )
    except ChatGptPromptSendError as exc:
        raise ChatGptSendStatusError(str(exc)) from exc
    with uow_factory() as uow:
        if uow.chatgpt_prompt_sends.has_event(command.event_id):
            return ChatGptSendStatusResult.DUPLICATE

        delivery = uow.prompt_deliveries.get(command.delivery_id)
        if delivery is None:
            raise UnknownChatGptSendDelivery(str(command.delivery_id))
        dispatch = uow.prompt_dispatches.get(delivery.dispatch_id)
        if dispatch is None:
            raise UnknownChatGptSendDelivery(str(command.delivery_id))
        if dispatch.agent_session != command.session:
            raise ChatGptSendSessionMismatch(command.session)

        projection = uow.chatgpt_prompt_sends.get(command.delivery_id)
        if projection is None:
            projection = ChatGptPromptSend.queued(
                delivery_id=command.delivery_id,
                session=command.session,
                now=delivery.acknowledged_at or delivery.updated_at,
            )

        recorded_at = datetime.now(timezone.utc)
        try:
            changed = projection.apply_event(
                event,
                recorded_at=recorded_at,
            )
            if changed:
                _promote_binding(
                    command,
                    uow=uow,
                    recorded_at=recorded_at,
                )
                uow.chatgpt_prompt_sends.save(projection)
        except ChatGptSendStatusError:
            raise
        except (ChatGptPromptSendError, ConversationBindingError) as exc:
            raise ChatGptSendStatusError(str(exc)) from exc
        uow.chatgpt_prompt_sends.add_event(event)
        uow.commit()
        return (
            ChatGptSendStatusResult.APPLIED
            if changed
            else ChatGptSendStatusResult.STALE
        )
