from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class ConversationBindingError(ValueError):
    """Base error for invalid ConversationBinding operations."""


class ConversationBindingState(StrEnum):
    BOUND = "BOUND"
    INVALIDATED = "INVALIDATED"


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ConversationBindingError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_agent_session(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 450
        or len(value.split(":")) != 3
        or any(not part for part in value.split(":"))
    ):
        raise ConversationBindingError("agent_session must be a canonical <project>:<role>:<work-item>")
    return value


def _validate_conversation_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 500
        or "/" in value
        or any(character.isspace() for character in value)
    ):
        raise ConversationBindingError("conversation_id must be a non-empty opaque path segment")
    return value


def normalize_chatgpt_conversation_url(
    raw_url: str,
    *,
    expected_conversation_id: str | None = None,
) -> str:
    if not isinstance(raw_url, str) or raw_url != raw_url.strip() or not raw_url:
        raise ConversationBindingError("canonical_url must be a non-empty URL")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise ConversationBindingError("canonical_url is invalid") from exc
    if parsed.scheme.lower() != "https:"[:-1]:
        raise ConversationBindingError("canonical_url must use https")
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"chatgpt.com", "chat.openai.com"} or port not in {None, 443}:
        raise ConversationBindingError("canonical_url must target a supported ChatGPT host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "c":
        raise ConversationBindingError("canonical_url must identify exactly one /c/<conversation-id>")
    conversation_id = _validate_conversation_id(parts[1])
    if expected_conversation_id is not None:
        expected = _validate_conversation_id(expected_conversation_id)
        if conversation_id != expected:
            raise ConversationBindingError("canonical_url conversation id does not match conversation_id")
    return f"https://chatgpt.com/c/{conversation_id}"


class ConversationBinding:
    """Durable mapping from one AgentSession to one exact ChatGPT conversation."""

    def __init__(
        self,
        *,
        binding_id: UUID,
        agent_session: str,
        conversation_id: str,
        canonical_url: str,
        state: ConversationBindingState,
        version: int,
        bound_at: datetime,
        last_validated_at: datetime,
        updated_at: datetime,
        invalidated_at: datetime | None,
        invalidation_reason: str | None,
    ) -> None:
        agent_session = _validate_agent_session(agent_session)
        conversation_id = _validate_conversation_id(conversation_id)
        canonical_url = normalize_chatgpt_conversation_url(
            canonical_url,
            expected_conversation_id=conversation_id,
        )
        bound_at = _as_utc(bound_at, field_name="bound_at")
        last_validated_at = _as_utc(last_validated_at, field_name="last_validated_at")
        updated_at = _as_utc(updated_at, field_name="updated_at")
        invalidated_at = (
            _as_utc(invalidated_at, field_name="invalidated_at")
            if invalidated_at is not None
            else None
        )
        if version < 1:
            raise ConversationBindingError("version must be at least 1")
        if last_validated_at < bound_at or updated_at < bound_at:
            raise ConversationBindingError("binding timestamps must not precede bound_at")
        if state is ConversationBindingState.BOUND:
            if invalidated_at is not None or invalidation_reason is not None:
                raise ConversationBindingError("BOUND binding cannot carry invalidation metadata")
        else:
            if invalidated_at is None:
                raise ConversationBindingError("INVALIDATED binding requires invalidated_at")
            if (
                not isinstance(invalidation_reason, str)
                or not invalidation_reason.strip()
                or invalidation_reason != invalidation_reason.strip()
            ):
                raise ConversationBindingError("INVALIDATED binding requires an explicit reason")
            if invalidated_at < bound_at or updated_at < invalidated_at:
                raise ConversationBindingError("invalidation timestamps are inconsistent")

        self._binding_id = binding_id
        self._agent_session = agent_session
        self._conversation_id = conversation_id
        self._canonical_url = canonical_url
        self._state = state
        self._version = version
        self._bound_at = bound_at
        self._last_validated_at = last_validated_at
        self._updated_at = updated_at
        self._invalidated_at = invalidated_at
        self._invalidation_reason = invalidation_reason

    @classmethod
    def bind(
        cls,
        *,
        agent_session: str,
        conversation_id: str,
        canonical_url: str,
        binding_id: UUID | None = None,
        now: datetime | None = None,
    ) -> "ConversationBinding":
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        return cls(
            binding_id=binding_id or uuid4(),
            agent_session=agent_session,
            conversation_id=conversation_id,
            canonical_url=canonical_url,
            state=ConversationBindingState.BOUND,
            version=1,
            bound_at=timestamp,
            last_validated_at=timestamp,
            updated_at=timestamp,
            invalidated_at=None,
            invalidation_reason=None,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        binding_id: UUID,
        agent_session: str,
        conversation_id: str,
        canonical_url: str,
        state: ConversationBindingState | str,
        version: int,
        bound_at: datetime,
        last_validated_at: datetime,
        updated_at: datetime,
        invalidated_at: datetime | None,
        invalidation_reason: str | None,
    ) -> "ConversationBinding":
        try:
            target_state = state if isinstance(state, ConversationBindingState) else ConversationBindingState(state)
        except ValueError as exc:
            raise ConversationBindingError("Persisted ConversationBinding has an invalid state") from exc
        return cls(
            binding_id=binding_id,
            agent_session=agent_session,
            conversation_id=conversation_id,
            canonical_url=canonical_url,
            state=target_state,
            version=version,
            bound_at=bound_at,
            last_validated_at=last_validated_at,
            updated_at=updated_at,
            invalidated_at=invalidated_at,
            invalidation_reason=invalidation_reason,
        )

    @property
    def binding_id(self) -> UUID:
        return self._binding_id

    @property
    def agent_session(self) -> str:
        return self._agent_session

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def canonical_url(self) -> str:
        return self._canonical_url

    @property
    def state(self) -> ConversationBindingState:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    @property
    def bound_at(self) -> datetime:
        return self._bound_at

    @property
    def last_validated_at(self) -> datetime:
        return self._last_validated_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def invalidated_at(self) -> datetime | None:
        return self._invalidated_at

    @property
    def invalidation_reason(self) -> str | None:
        return self._invalidation_reason

    def rebind(
        self,
        *,
        conversation_id: str,
        canonical_url: str,
        now: datetime | None = None,
    ) -> None:
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._updated_at:
            raise ConversationBindingError("rebind timestamp must not move backwards")
        conversation_id = _validate_conversation_id(conversation_id)
        canonical_url = normalize_chatgpt_conversation_url(
            canonical_url,
            expected_conversation_id=conversation_id,
        )
        self._conversation_id = conversation_id
        self._canonical_url = canonical_url
        self._state = ConversationBindingState.BOUND
        self._version += 1
        self._bound_at = timestamp
        self._last_validated_at = timestamp
        self._updated_at = timestamp
        self._invalidated_at = None
        self._invalidation_reason = None

    def mark_validated(self, *, now: datetime | None = None) -> None:
        if self._state is not ConversationBindingState.BOUND:
            raise ConversationBindingError("Only a BOUND binding can be validated")
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._updated_at:
            raise ConversationBindingError("validation timestamp must not move backwards")
        self._version += 1
        self._last_validated_at = timestamp
        self._updated_at = timestamp

    def invalidate(self, reason: str, *, now: datetime | None = None) -> bool:
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or reason != reason.strip()
            or len(reason) > 1000
        ):
            raise ConversationBindingError("invalidation reason must be explicit and non-blank")
        if self._state is ConversationBindingState.INVALIDATED:
            return False
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._updated_at:
            raise ConversationBindingError("invalidation timestamp must not move backwards")
        self._state = ConversationBindingState.INVALIDATED
        self._version += 1
        self._updated_at = timestamp
        self._invalidated_at = timestamp
        self._invalidation_reason = reason
        return True
