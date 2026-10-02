from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.resource_lock import (
    ConflictSurface,
    ResourceLock,
    ResourceLockConflict,
    ResourceLockMode,
    ResourceLockRequirement,
    ResourceLockState,
    lock_modes_compatible,
)
from app.infrastructure.database import Base


class ResourceLockConcurrencyError(RuntimeError):
    """Raised when a ResourceLock version changed unexpectedly."""


class ResourceLockRecord(Base):
    __tablename__ = "resource_locks"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "work_item_id",
            "surface",
            name="uq_resource_lock_owner_surface",
        ),
        CheckConstraint(
            "mode IN ('SHARED','EXCLUSIVE')",
            name="ck_resource_lock_mode",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','RELEASED','STALE')",
            name="ck_resource_lock_state",
        ),
        CheckConstraint(
            "version >= 1 AND length(trim(project_id)) > 0 "
            "AND length(trim(work_item_id)) > 0 AND length(trim(agent_session)) > 0 "
            "AND length(trim(lease_owner_id)) > 0 AND length(trim(surface)) > 0",
            name="ck_resource_lock_identity",
        ),
        CheckConstraint(
            "(state = 'ACTIVE' AND released_at IS NULL AND release_reason IS NULL) "
            "OR (state IN ('RELEASED','STALE') AND released_at IS NOT NULL "
            "AND release_reason IS NOT NULL AND length(trim(release_reason)) > 0)",
            name="ck_resource_lock_release",
        ),
        Index(
            "ix_resource_locks_project_state_surface",
            "project_id",
            "state",
            "surface",
        ),
        Index(
            "ix_resource_locks_owner_state",
            "project_id",
            "work_item_id",
            "state",
        ),
    )

    lock_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_session: Mapped[str] = mapped_column(String(450), nullable=False)
    lease_owner_id: Mapped[str] = mapped_column(String(100), nullable=False)
    surface: Mapped[str] = mapped_column(String(400), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SqlAlchemyResourceLockRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, lock_id: UUID | str) -> ResourceLock | None:
        record = self._session.get(ResourceLockRecord, str(lock_id))
        return _domain_from_record(record) if record is not None else None

    def get_for_owner_surface(
        self,
        project_id: str,
        work_item_id: str,
        surface: str,
    ) -> ResourceLock | None:
        record = self._session.scalar(
            select(ResourceLockRecord).where(
                ResourceLockRecord.project_id == project_id,
                ResourceLockRecord.work_item_id == work_item_id,
                ResourceLockRecord.surface == surface,
            )
        )
        return _domain_from_record(record) if record is not None else None

    def list_for_project(self, project_id: str) -> tuple[ResourceLock, ...]:
        records = self._session.scalars(
            select(ResourceLockRecord)
            .where(ResourceLockRecord.project_id == project_id)
            .order_by(
                ResourceLockRecord.surface,
                ResourceLockRecord.work_item_id,
                ResourceLockRecord.lock_id,
            )
        ).all()
        return tuple(_domain_from_record(record) for record in records)

    def list_active_for_project(self, project_id: str) -> tuple[ResourceLock, ...]:
        records = self._session.scalars(
            select(ResourceLockRecord)
            .where(
                ResourceLockRecord.project_id == project_id,
                ResourceLockRecord.state == ResourceLockState.ACTIVE.value,
            )
            .order_by(
                ResourceLockRecord.surface,
                ResourceLockRecord.work_item_id,
                ResourceLockRecord.lock_id,
            )
        ).all()
        return tuple(_domain_from_record(record) for record in records)

    def list_for_owner(
        self,
        project_id: str,
        work_item_id: str,
    ) -> tuple[ResourceLock, ...]:
        records = self._session.scalars(
            select(ResourceLockRecord)
            .where(
                ResourceLockRecord.project_id == project_id,
                ResourceLockRecord.work_item_id == work_item_id,
            )
            .order_by(ResourceLockRecord.surface, ResourceLockRecord.lock_id)
        ).all()
        return tuple(_domain_from_record(record) for record in records)

    def save(self, lock: ResourceLock) -> None:
        record = self._session.get(ResourceLockRecord, str(lock.lock_id))
        if record is None:
            if lock.version != 1:
                raise ResourceLockConcurrencyError(
                    "new ResourceLock must start at version 1"
                )
            self._session.add(_record_from_domain(lock))
            return

        expected_version = lock.version - 1
        if record.version != expected_version:
            raise ResourceLockConcurrencyError(
                f"ResourceLock version conflict: expected {expected_version}, "
                f"found {record.version}"
            )
        record.agent_session = lock.agent_session
        record.lease_owner_id = lock.lease_owner_id
        record.mode = lock.mode.value
        record.state = lock.state.value
        record.acquired_at = lock.acquired_at
        record.lease_expires_at = lock.lease_expires_at
        record.updated_at = lock.updated_at
        record.version = lock.version
        record.released_at = lock.released_at
        record.release_reason = lock.release_reason

    def acquire_many(
        self,
        *,
        project_id: str,
        work_item_id: str,
        agent_session: str,
        lease_owner_id: str,
        requirements: Iterable[ResourceLockRequirement],
        now: datetime,
        lease_seconds: float,
    ) -> tuple[tuple[ResourceLock, ...], ResourceLockConflict | None]:
        ordered = tuple(requirements)
        if not ordered:
            return (), None

        surfaces = tuple(requirement.surface.key for requirement in ordered)
        holders = self._session.scalars(
            select(ResourceLockRecord)
            .where(
                ResourceLockRecord.project_id == project_id,
                ResourceLockRecord.state == ResourceLockState.ACTIVE.value,
                ResourceLockRecord.surface.in_(surfaces),
                ResourceLockRecord.work_item_id != work_item_id,
            )
            .order_by(
                ResourceLockRecord.surface,
                ResourceLockRecord.work_item_id,
                ResourceLockRecord.lock_id,
            )
        ).all()
        by_surface: dict[str, list[ResourceLock]] = {}
        for record in holders:
            holder = _domain_from_record(record)
            by_surface.setdefault(holder.surface.key, []).append(holder)

        for requirement in ordered:
            for holder in by_surface.get(requirement.surface.key, ()):
                if not lock_modes_compatible(requirement.mode, holder.mode):
                    return (), ResourceLockConflict(
                        surface=requirement.surface,
                        requested_mode=requirement.mode,
                        holder_work_item_id=holder.work_item_id,
                        holder_agent_session=holder.agent_session,
                        holder_mode=holder.mode,
                        holder_state=holder.state,
                    )

        acquired: list[ResourceLock] = []
        for requirement in ordered:
            existing = self.get_for_owner_surface(
                project_id,
                work_item_id,
                requirement.surface.key,
            )
            if existing is None:
                lock = ResourceLock.acquire(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    agent_session=agent_session,
                    lease_owner_id=lease_owner_id,
                    requirement=requirement,
                    now=now,
                    lease_seconds=lease_seconds,
                )
            else:
                lock = existing.reactivate(
                    agent_session=agent_session,
                    lease_owner_id=lease_owner_id,
                    requirement=requirement,
                    now=now,
                    lease_seconds=lease_seconds,
                )
            self.save(lock)
            acquired.append(lock)

        # The UnitOfWork uses autoflush=False. Flush here so a second candidate
        # evaluated in the same BEGIN IMMEDIATE transaction observes these locks.
        self._session.flush()
        return tuple(acquired), None


def _record_from_domain(lock: ResourceLock) -> ResourceLockRecord:
    return ResourceLockRecord(
        lock_id=str(lock.lock_id),
        project_id=lock.project_id,
        work_item_id=lock.work_item_id,
        agent_session=lock.agent_session,
        lease_owner_id=lock.lease_owner_id,
        surface=lock.surface.key,
        mode=lock.mode.value,
        state=lock.state.value,
        acquired_at=lock.acquired_at,
        lease_expires_at=lock.lease_expires_at,
        updated_at=lock.updated_at,
        version=lock.version,
        released_at=lock.released_at,
        release_reason=lock.release_reason,
    )


def _domain_from_record(record: ResourceLockRecord) -> ResourceLock:
    return ResourceLock.rehydrate(
        lock_id=record.lock_id,
        project_id=record.project_id,
        work_item_id=record.work_item_id,
        agent_session=record.agent_session,
        lease_owner_id=record.lease_owner_id,
        surface=ConflictSurface(record.surface),
        mode=ResourceLockMode(record.mode),
        state=ResourceLockState(record.state),
        acquired_at=_as_utc(record.acquired_at),
        lease_expires_at=_as_utc(record.lease_expires_at),
        updated_at=_as_utc(record.updated_at),
        version=record.version,
        released_at=_as_utc(record.released_at) if record.released_at is not None else None,
        release_reason=record.release_reason,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
