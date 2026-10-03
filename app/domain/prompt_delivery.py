from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4


class PromptDeliveryError(ValueError):
    """Base error for invalid prompt-delivery operations."""


class InvalidPromptDeliveryTransition(PromptDeliveryError):
    """Raised when a transport delivery transition is not allowed."""


class PromptDeliveryStatus(StrEnum):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise PromptDeliveryError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class PromptDelivery:
    """Persistent transport truth for one PromptDispatch."""

    def __init__(
        self,
        *,
        delivery_id: UUID,
        dispatch_id: UUID,
        status: PromptDeliveryStatus,
        attempt_count: int,
        last_attempt_at: datetime | None,
        acknowledged_at: datetime | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        created = _as_utc(created_at, field_name="created_at")
        updated = _as_utc(updated_at, field_name="updated_at")
        last_attempt = (
            _as_utc(last_attempt_at, field_name="last_attempt_at")
            if last_attempt_at is not None
            else None
        )
        acknowledged = (
            _as_utc(acknowledged_at, field_name="acknowledged_at")
            if acknowledged_at is not None
            else None
        )

        if attempt_count < 0:
            raise PromptDeliveryError("attempt_count must not be negative")
        if (attempt_count == 0) != (last_attempt is None):
            raise PromptDeliveryError(
                "last_attempt_at must be absent only when attempt_count is zero"
            )
        if updated < created:
            raise PromptDeliveryError("updated_at must not precede created_at")
        if last_attempt is not None and last_attempt < created:
            raise PromptDeliveryError("last_attempt_at must not precede created_at")
        if acknowledged is not None and acknowledged < created:
            raise PromptDeliveryError("acknowledged_at must not precede created_at")
        if status is PromptDeliveryStatus.PENDING and acknowledged is not None:
            raise PromptDeliveryError("PENDING delivery must not have acknowledged_at")
        if status is PromptDeliveryStatus.ACKNOWLEDGED:
            if acknowledged is None:
                raise PromptDeliveryError("ACKNOWLEDGED delivery requires acknowledged_at")
            if attempt_count == 0:
                raise PromptDeliveryError("ACKNOWLEDGED delivery requires a recorded attempt")

        self._delivery_id = delivery_id
        self._dispatch_id = dispatch_id
        self._status = status
        self._attempt_count = attempt_count
        self._last_attempt_at = last_attempt
        self._acknowledged_at = acknowledged
        self._created_at = created
        self._updated_at = updated

    @classmethod
    def create(
        cls,
        *,
        dispatch_id: UUID,
        delivery_id: UUID | None = None,
        now: datetime | None = None,
    ) -> PromptDelivery:
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        return cls(
            delivery_id=delivery_id or uuid4(),
            dispatch_id=dispatch_id,
            status=PromptDeliveryStatus.PENDING,
            attempt_count=0,
            last_attempt_at=None,
            acknowledged_at=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        delivery_id: UUID,
        dispatch_id: UUID,
        status: PromptDeliveryStatus | str,
        attempt_count: int,
        last_attempt_at: datetime | None,
        acknowledged_at: datetime | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> PromptDelivery:
        try:
            target_status = (
                status if isinstance(status, PromptDeliveryStatus) else PromptDeliveryStatus(status)
            )
        except ValueError as exc:
            raise PromptDeliveryError("Persisted PromptDelivery has an invalid status") from exc
        return cls(
            delivery_id=delivery_id,
            dispatch_id=dispatch_id,
            status=target_status,
            attempt_count=attempt_count,
            last_attempt_at=last_attempt_at,
            acknowledged_at=acknowledged_at,
            created_at=created_at,
            updated_at=updated_at,
        )

    @property
    def delivery_id(self) -> UUID:
        return self._delivery_id

    @property
    def dispatch_id(self) -> UUID:
        return self._dispatch_id

    @property
    def status(self) -> PromptDeliveryStatus:
        return self._status

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def last_attempt_at(self) -> datetime | None:
        return self._last_attempt_at

    @property
    def acknowledged_at(self) -> datetime | None:
        return self._acknowledged_at

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def is_acknowledged(self) -> bool:
        return self._status is PromptDeliveryStatus.ACKNOWLEDGED

    def record_attempt(self, *, now: datetime | None = None) -> None:
        if self.is_acknowledged:
            raise InvalidPromptDeliveryTransition(
                "Cannot record another transport attempt after acknowledgement"
            )
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._updated_at:
            raise PromptDeliveryError("attempt timestamp must not move backwards")
        self._attempt_count += 1
        self._last_attempt_at = timestamp
        self._updated_at = timestamp

    def record_redelivery_attempt(self, *, now: datetime | None = None) -> None:
        """Record an explicit resend while preserving the historical ACK fact."""

        if not self.is_acknowledged:
            raise InvalidPromptDeliveryTransition(
                "Manual redelivery requires an acknowledged delivery"
            )
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._updated_at:
            raise PromptDeliveryError("redelivery timestamp must not move backwards")
        self._attempt_count += 1
        self._last_attempt_at = timestamp
        self._updated_at = timestamp

    def acknowledge(self, *, now: datetime | None = None) -> bool:
        if self.is_acknowledged:
            return False
        if self._attempt_count == 0 or self._last_attempt_at is None:
            raise InvalidPromptDeliveryTransition(
                "Cannot acknowledge a delivery before a transport attempt"
            )
        timestamp = _as_utc(now or _utc_now(), field_name="now")
        if timestamp < self._last_attempt_at:
            raise PromptDeliveryError("acknowledgement timestamp must not precede last attempt")
        self._status = PromptDeliveryStatus.ACKNOWLEDGED
        self._acknowledged_at = timestamp
        self._updated_at = timestamp
        return True
