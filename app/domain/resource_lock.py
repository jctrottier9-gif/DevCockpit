from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID, uuid4


class ResourceLockMode(StrEnum):
    SHARED = "SHARED"
    EXCLUSIVE = "EXCLUSIVE"


class ResourceLockState(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class ConflictSurface:
    key: str

    def __post_init__(self) -> None:
        normalized = self.key.strip()
        if not normalized or len(normalized) > 400:
            raise ValueError("ConflictSurface key must contain 1 to 400 characters")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("ConflictSurface key must not contain control characters")
        object.__setattr__(self, "key", normalized)


@dataclass(frozen=True, slots=True)
class ResourceLockRequirement:
    surface: ConflictSurface
    mode: ResourceLockMode

    @classmethod
    def build(
        cls,
        surface: ConflictSurface | str,
        mode: ResourceLockMode | str = ResourceLockMode.EXCLUSIVE,
    ) -> "ResourceLockRequirement":
        return cls(
            surface=surface if isinstance(surface, ConflictSurface) else ConflictSurface(surface),
            mode=mode if isinstance(mode, ResourceLockMode) else ResourceLockMode(mode),
        )


@dataclass(frozen=True, slots=True)
class WorkItemResourceLockDeclaration:
    work_item_id: str
    requirements: tuple[ResourceLockRequirement, ...]

    def __post_init__(self) -> None:
        identity = self.work_item_id.strip()
        if not identity or len(identity) > 200:
            raise ValueError("work_item_id must contain 1 to 200 characters")
        surfaces = [requirement.surface.key for requirement in self.requirements]
        if len(surfaces) != len(set(surfaces)):
            raise ValueError(f"duplicate ResourceLock surface for {identity}")
        object.__setattr__(self, "work_item_id", identity)


@dataclass(frozen=True, slots=True)
class ResourceLock:
    lock_id: UUID
    project_id: str
    work_item_id: str
    agent_session: str
    surface: ConflictSurface
    mode: ResourceLockMode
    state: ResourceLockState
    acquired_at: datetime
    lease_expires_at: datetime
    updated_at: datetime
    version: int
    released_at: datetime | None = None
    release_reason: str | None = None

    @classmethod
    def acquire(
        cls,
        *,
        project_id: str,
        work_item_id: str,
        agent_session: str,
        requirement: ResourceLockRequirement,
        now: datetime,
        lease_seconds: float,
    ) -> "ResourceLock":
        current = _as_utc(now)
        return cls(
            lock_id=uuid4(),
            project_id=project_id,
            work_item_id=work_item_id,
            agent_session=agent_session,
            surface=requirement.surface,
            mode=requirement.mode,
            state=ResourceLockState.ACTIVE,
            acquired_at=current,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
            updated_at=current,
            version=1,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        lock_id: UUID | str,
        project_id: str,
        work_item_id: str,
        agent_session: str,
        surface: ConflictSurface | str,
        mode: ResourceLockMode | str,
        state: ResourceLockState | str,
        acquired_at: datetime,
        lease_expires_at: datetime,
        updated_at: datetime,
        version: int,
        released_at: datetime | None,
        release_reason: str | None,
    ) -> "ResourceLock":
        return cls(
            lock_id=lock_id if isinstance(lock_id, UUID) else UUID(lock_id),
            project_id=project_id,
            work_item_id=work_item_id,
            agent_session=agent_session,
            surface=surface if isinstance(surface, ConflictSurface) else ConflictSurface(surface),
            mode=mode if isinstance(mode, ResourceLockMode) else ResourceLockMode(mode),
            state=state if isinstance(state, ResourceLockState) else ResourceLockState(state),
            acquired_at=_as_utc(acquired_at),
            lease_expires_at=_as_utc(lease_expires_at),
            updated_at=_as_utc(updated_at),
            version=version,
            released_at=_as_utc(released_at) if released_at is not None else None,
            release_reason=release_reason,
        )

    def expired(self, now: datetime) -> bool:
        return self.state is ResourceLockState.ACTIVE and self.lease_expires_at <= _as_utc(now)

    def renew(self, *, now: datetime, lease_seconds: float) -> "ResourceLock":
        if self.state is not ResourceLockState.ACTIVE:
            raise ValueError("only ACTIVE ResourceLocks can be renewed")
        current = _as_utc(now)
        return replace(
            self,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
            updated_at=current,
            version=self.version + 1,
        )

    def release(self, *, now: datetime, reason: str) -> "ResourceLock":
        if self.state is not ResourceLockState.ACTIVE:
            return self
        current = _as_utc(now)
        return replace(
            self,
            state=ResourceLockState.RELEASED,
            updated_at=current,
            version=self.version + 1,
            released_at=current,
            release_reason=reason,
        )

    def mark_stale(self, *, now: datetime, reason: str) -> "ResourceLock":
        if self.state is not ResourceLockState.ACTIVE:
            return self
        current = _as_utc(now)
        return replace(
            self,
            state=ResourceLockState.STALE,
            updated_at=current,
            version=self.version + 1,
            released_at=current,
            release_reason=reason,
        )

    def reactivate(
        self,
        *,
        agent_session: str,
        requirement: ResourceLockRequirement,
        now: datetime,
        lease_seconds: float,
    ) -> "ResourceLock":
        if self.state is ResourceLockState.ACTIVE:
            if self.mode is not requirement.mode or self.agent_session != agent_session:
                raise ValueError("ACTIVE ResourceLock cannot change owner session or mode")
            return self.renew(now=now, lease_seconds=lease_seconds)
        current = _as_utc(now)
        return replace(
            self,
            agent_session=agent_session,
            mode=requirement.mode,
            state=ResourceLockState.ACTIVE,
            acquired_at=current,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
            updated_at=current,
            version=self.version + 1,
            released_at=None,
            release_reason=None,
        )


@dataclass(frozen=True, slots=True)
class ResourceLockConflict:
    surface: ConflictSurface
    requested_mode: ResourceLockMode
    holder_work_item_id: str
    holder_agent_session: str
    holder_mode: ResourceLockMode
    holder_state: ResourceLockState
    reason: str = "INCOMPATIBLE_RESOURCE_LOCK"


def lock_modes_compatible(left: ResourceLockMode, right: ResourceLockMode) -> bool:
    return left is ResourceLockMode.SHARED and right is ResourceLockMode.SHARED


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
