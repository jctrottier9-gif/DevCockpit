from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID


class ChatGptPromptSendError(ValueError):
    """Base error for persisted browser-to-ChatGPT send state."""


class InvalidChatGptPromptSendTransition(ChatGptPromptSendError):
    """Raised when a status event would violate fail-stop send semantics."""


class ChatGptPromptSendState(StrEnum):
    QUEUED = "QUEUED"
    ROUTING = "ROUTING"
    WAITING_READY = "WAITING_READY"
    SEND_ARMED = "SEND_ARMED"
    SENT_CONFIRMED = "SENT_CONFIRMED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    BLOCKED = "BLOCKED"
    AMBIGUOUS = "AMBIGUOUS"


_TERMINAL_STATES = frozenset(
    {
        ChatGptPromptSendState.SENT_CONFIRMED,
        ChatGptPromptSendState.BLOCKED,
        ChatGptPromptSendState.AMBIGUOUS,
    }
)
_ALLOWED_TRANSITIONS = {
    ChatGptPromptSendState.QUEUED: {
        ChatGptPromptSendState.ROUTING,
        ChatGptPromptSendState.WAITING_READY,
        ChatGptPromptSendState.RETRYABLE_FAILURE,
        ChatGptPromptSendState.BLOCKED,
    },
    ChatGptPromptSendState.ROUTING: {
        ChatGptPromptSendState.WAITING_READY,
        ChatGptPromptSendState.RETRYABLE_FAILURE,
        ChatGptPromptSendState.BLOCKED,
        ChatGptPromptSendState.SEND_ARMED,
    },
    ChatGptPromptSendState.WAITING_READY: {
        ChatGptPromptSendState.ROUTING,
        ChatGptPromptSendState.RETRYABLE_FAILURE,
        ChatGptPromptSendState.BLOCKED,
        ChatGptPromptSendState.SEND_ARMED,
    },
    ChatGptPromptSendState.RETRYABLE_FAILURE: {
        ChatGptPromptSendState.ROUTING,
        ChatGptPromptSendState.WAITING_READY,
        ChatGptPromptSendState.BLOCKED,
    },
    ChatGptPromptSendState.SEND_ARMED: {
        ChatGptPromptSendState.SENT_CONFIRMED,
        ChatGptPromptSendState.AMBIGUOUS,
    },
    ChatGptPromptSendState.SENT_CONFIRMED: set(),
    ChatGptPromptSendState.BLOCKED: set(),
    ChatGptPromptSendState.AMBIGUOUS: set(),
}


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ChatGptPromptSendError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ChatGptSendStatusEvent:
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

    def __post_init__(self) -> None:
        if not self.session or self.session != self.session.strip():
            raise ChatGptPromptSendError("session must be non-blank and canonical")
        if self.attempt_count < 0:
            raise ChatGptPromptSendError("attempt_count must not be negative")
        object.__setattr__(
            self,
            "occurred_at",
            _as_utc(self.occurred_at, field_name="occurred_at"),
        )
        if self.next_retry_at is not None:
            object.__setattr__(
                self,
                "next_retry_at",
                _as_utc(self.next_retry_at, field_name="next_retry_at"),
            )
        if (self.conversation_id is None) != (self.canonical_url is None):
            raise ChatGptPromptSendError(
                "conversation_id and canonical_url must be supplied together"
            )
        if self.state is ChatGptPromptSendState.SENT_CONFIRMED and (
            not self.conversation_id or not self.canonical_url
        ):
            raise ChatGptPromptSendError(
                "SENT_CONFIRMED requires exact conversation identity"
            )


class ChatGptPromptSend:
    """Backend projection of one durable Firefox-to-ChatGPT send."""

    def __init__(
        self,
        *,
        delivery_id: UUID,
        session: str,
        state: ChatGptPromptSendState,
        attempt_count: int,
        last_error_code: str | None,
        next_retry_at: datetime | None,
        confirmed_at: datetime | None,
        updated_at: datetime,
        last_event_id: UUID | None,
    ) -> None:
        if not session or session != session.strip():
            raise ChatGptPromptSendError("session must be non-blank and canonical")
        if attempt_count < 0:
            raise ChatGptPromptSendError("attempt_count must not be negative")
        updated_at = _as_utc(updated_at, field_name="updated_at")
        next_retry_at = (
            _as_utc(next_retry_at, field_name="next_retry_at")
            if next_retry_at is not None
            else None
        )
        confirmed_at = (
            _as_utc(confirmed_at, field_name="confirmed_at")
            if confirmed_at is not None
            else None
        )
        if state is ChatGptPromptSendState.SENT_CONFIRMED and confirmed_at is None:
            raise ChatGptPromptSendError("SENT_CONFIRMED requires confirmed_at")
        if state is not ChatGptPromptSendState.SENT_CONFIRMED and confirmed_at is not None:
            raise ChatGptPromptSendError("Only SENT_CONFIRMED may carry confirmed_at")

        self._delivery_id = delivery_id
        self._session = session
        self._state = state
        self._attempt_count = attempt_count
        self._last_error_code = last_error_code
        self._next_retry_at = next_retry_at
        self._confirmed_at = confirmed_at
        self._updated_at = updated_at
        self._last_event_id = last_event_id

    @classmethod
    def queued(
        cls,
        *,
        delivery_id: UUID,
        session: str,
        now: datetime,
    ) -> "ChatGptPromptSend":
        timestamp = _as_utc(now, field_name="now")
        return cls(
            delivery_id=delivery_id,
            session=session,
            state=ChatGptPromptSendState.QUEUED,
            attempt_count=0,
            last_error_code=None,
            next_retry_at=None,
            confirmed_at=None,
            updated_at=timestamp,
            last_event_id=None,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        delivery_id: UUID,
        session: str,
        state: ChatGptPromptSendState | str,
        attempt_count: int,
        last_error_code: str | None,
        next_retry_at: datetime | None,
        confirmed_at: datetime | None,
        updated_at: datetime,
        last_event_id: UUID | None,
    ) -> "ChatGptPromptSend":
        return cls(
            delivery_id=delivery_id,
            session=session,
            state=state if isinstance(state, ChatGptPromptSendState) else ChatGptPromptSendState(state),
            attempt_count=attempt_count,
            last_error_code=last_error_code,
            next_retry_at=next_retry_at,
            confirmed_at=confirmed_at,
            updated_at=updated_at,
            last_event_id=last_event_id,
        )

    @property
    def delivery_id(self) -> UUID:
        return self._delivery_id

    @property
    def session(self) -> str:
        return self._session

    @property
    def state(self) -> ChatGptPromptSendState:
        return self._state

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def last_error_code(self) -> str | None:
        return self._last_error_code

    @property
    def next_retry_at(self) -> datetime | None:
        return self._next_retry_at

    @property
    def confirmed_at(self) -> datetime | None:
        return self._confirmed_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def last_event_id(self) -> UUID | None:
        return self._last_event_id

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    def apply_event(self, event: ChatGptSendStatusEvent) -> bool:
        if event.delivery_id != self._delivery_id or event.session != self._session:
            raise ChatGptPromptSendError("status event identity does not match projection")
        if event.occurred_at < self._updated_at:
            return False
        if event.attempt_count < self._attempt_count:
            return False
        if self.is_terminal:
            return False

        if event.state is not self._state and event.state not in _ALLOWED_TRANSITIONS[self._state]:
            raise InvalidChatGptPromptSendTransition(
                f"{self._state.value} -> {event.state.value} is not allowed"
            )
        if self._state is ChatGptPromptSendState.SEND_ARMED and (
            event.state
            not in {
                ChatGptPromptSendState.SEND_ARMED,
                ChatGptPromptSendState.SENT_CONFIRMED,
                ChatGptPromptSendState.AMBIGUOUS,
            }
        ):
            raise InvalidChatGptPromptSendTransition(
                "SEND_ARMED may only confirm or become AMBIGUOUS"
            )

        self._state = event.state
        self._attempt_count = max(self._attempt_count, event.attempt_count)
        self._last_error_code = event.error_code
        self._next_retry_at = event.next_retry_at
        self._confirmed_at = (
            event.occurred_at
            if event.state is ChatGptPromptSendState.SENT_CONFIRMED
            else None
        )
        self._updated_at = event.occurred_at
        self._last_event_id = event.event_id
        return True
