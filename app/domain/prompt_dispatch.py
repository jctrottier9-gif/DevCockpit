from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import re
from uuid import UUID, uuid4


_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_WORK_ITEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class PromptDispatchError(ValueError):
    """Base error for invalid PromptDispatch domain operations."""


class InvalidPromptDispatchTransition(PromptDispatchError):
    """Raised when a PromptDispatch state transition is not allowed."""


class PromptDispatchRole(StrEnum):
    PO = "PO"
    ARCH = "ARCH"
    DEV = "DEV"


class PromptDispatchStatus(StrEnum):
    PREPARED = "PREPARED"
    CANCELLED = "CANCELLED"


_ALLOWED_TRANSITIONS: dict[PromptDispatchStatus, frozenset[PromptDispatchStatus]] = {
    PromptDispatchStatus.PREPARED: frozenset({PromptDispatchStatus.CANCELLED}),
    PromptDispatchStatus.CANCELLED: frozenset(),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_component(value: str, *, field_name: str, pattern: re.Pattern[str]) -> str:
    if not value or value != value.strip() or not pattern.fullmatch(value):
        raise PromptDispatchError(f"Invalid {field_name}: {value!r}")
    if ":" in value:
        raise PromptDispatchError(f"{field_name} must not contain ':'")
    return value


def build_agent_session(
    project_id: str,
    role: PromptDispatchRole | str,
    work_item_id: str,
) -> str:
    project = _validate_component(project_id, field_name="project_id", pattern=_COMPONENT_PATTERN)
    work_item = _validate_component(
        work_item_id,
        field_name="work_item_id",
        pattern=_WORK_ITEM_PATTERN,
    )
    try:
        target_role = role if isinstance(role, PromptDispatchRole) else PromptDispatchRole(role)
    except ValueError as exc:
        raise PromptDispatchError(f"Invalid role: {role!r}") from exc
    return f"{project}:{target_role.value}:{work_item}"


class PromptDispatch:
    """A prompt prepared for one logical AgentSession, independent of transport."""

    def __init__(
        self,
        *,
        dispatch_id: UUID,
        project_id: str,
        work_item_id: str,
        role: PromptDispatchRole,
        agent_session: str,
        prompt_text: str,
        status: PromptDispatchStatus,
        idempotency_key: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        expected_session = build_agent_session(project_id, role, work_item_id)
        if agent_session != expected_session:
            raise PromptDispatchError(
                f"agent_session must be {expected_session!r}, got {agent_session!r}"
            )
        if not prompt_text or not prompt_text.strip():
            raise PromptDispatchError("prompt_text must not be blank")
        if not idempotency_key or idempotency_key != idempotency_key.strip():
            raise PromptDispatchError("idempotency_key must not be blank or padded")
        if len(idempotency_key) > 200:
            raise PromptDispatchError("idempotency_key must be at most 200 characters")
        if created_at.tzinfo is None or updated_at.tzinfo is None:
            raise PromptDispatchError("timestamps must be timezone-aware")
        if updated_at < created_at:
            raise PromptDispatchError("updated_at must not precede created_at")

        self._dispatch_id = dispatch_id
        self._project_id = project_id
        self._work_item_id = work_item_id
        self._role = role
        self._agent_session = agent_session
        self._prompt_text = prompt_text
        self._status = status
        self._idempotency_key = idempotency_key
        self._created_at = created_at.astimezone(timezone.utc)
        self._updated_at = updated_at.astimezone(timezone.utc)

    @classmethod
    def prepare(
        cls,
        *,
        project_id: str,
        work_item_id: str,
        role: PromptDispatchRole | str,
        prompt_text: str,
        idempotency_key: str,
        dispatch_id: UUID | None = None,
        now: datetime | None = None,
    ) -> PromptDispatch:
        try:
            target_role = role if isinstance(role, PromptDispatchRole) else PromptDispatchRole(role)
        except ValueError as exc:
            raise PromptDispatchError(f"Invalid role: {role!r}") from exc
        timestamp = now or _utc_now()
        if timestamp.tzinfo is None:
            raise PromptDispatchError("now must be timezone-aware")
        agent_session = build_agent_session(project_id, target_role, work_item_id)
        return cls(
            dispatch_id=dispatch_id or uuid4(),
            project_id=project_id,
            work_item_id=work_item_id,
            role=target_role,
            agent_session=agent_session,
            prompt_text=prompt_text,
            status=PromptDispatchStatus.PREPARED,
            idempotency_key=idempotency_key,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        dispatch_id: UUID,
        project_id: str,
        work_item_id: str,
        role: PromptDispatchRole | str,
        agent_session: str,
        prompt_text: str,
        status: PromptDispatchStatus | str,
        idempotency_key: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> PromptDispatch:
        try:
            target_role = role if isinstance(role, PromptDispatchRole) else PromptDispatchRole(role)
            target_status = (
                status if isinstance(status, PromptDispatchStatus) else PromptDispatchStatus(status)
            )
        except ValueError as exc:
            raise PromptDispatchError("Persisted PromptDispatch contains an invalid enum value") from exc
        return cls(
            dispatch_id=dispatch_id,
            project_id=project_id,
            work_item_id=work_item_id,
            role=target_role,
            agent_session=agent_session,
            prompt_text=prompt_text,
            status=target_status,
            idempotency_key=idempotency_key,
            created_at=created_at,
            updated_at=updated_at,
        )

    @property
    def dispatch_id(self) -> UUID:
        return self._dispatch_id

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def work_item_id(self) -> str:
        return self._work_item_id

    @property
    def role(self) -> PromptDispatchRole:
        return self._role

    @property
    def agent_session(self) -> str:
        return self._agent_session

    @property
    def prompt_text(self) -> str:
        return self._prompt_text

    @property
    def status(self) -> PromptDispatchStatus:
        return self._status

    @property
    def idempotency_key(self) -> str:
        return self._idempotency_key

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    def transition_to(
        self,
        status: PromptDispatchStatus,
        *,
        now: datetime | None = None,
    ) -> None:
        if status not in _ALLOWED_TRANSITIONS[self._status]:
            raise InvalidPromptDispatchTransition(
                f"Cannot transition PromptDispatch from {self._status.value} to {status.value}"
            )
        timestamp = now or _utc_now()
        if timestamp.tzinfo is None:
            raise PromptDispatchError("now must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        if timestamp < self._updated_at:
            raise PromptDispatchError("transition timestamp must not move backwards")
        self._status = status
        self._updated_at = timestamp

    def cancel(self, *, now: datetime | None = None) -> None:
        self.transition_to(PromptDispatchStatus.CANCELLED, now=now)

    def represents_same_logical_prompt(
        self,
        *,
        project_id: str,
        work_item_id: str,
        role: PromptDispatchRole,
        prompt_text: str,
    ) -> bool:
        return (
            self.project_id == project_id
            and self.work_item_id == work_item_id
            and self.role == role
            and self.prompt_text == prompt_text
            and self.agent_session == build_agent_session(project_id, role, work_item_id)
        )
