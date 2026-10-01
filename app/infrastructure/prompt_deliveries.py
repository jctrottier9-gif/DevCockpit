from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.prompt_delivery import PromptDelivery
from app.infrastructure.database import Base


class PromptDeliveryRecord(Base):
    __tablename__ = "prompt_deliveries"
    __table_args__ = (
        UniqueConstraint("dispatch_id", name="uq_prompt_deliveries_dispatch_id"),
        ForeignKeyConstraint(
            ["dispatch_id"],
            ["prompt_dispatches.dispatch_id"],
            name="fk_prompt_deliveries_dispatch_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'ACKNOWLEDGED')",
            name="ck_prompt_deliveries_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_prompt_deliveries_attempt_count"),
        CheckConstraint(
            "(attempt_count = 0 AND last_attempt_at IS NULL) OR "
            "(attempt_count > 0 AND last_attempt_at IS NOT NULL)",
            name="ck_prompt_deliveries_attempt_timestamp",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND acknowledged_at IS NULL) OR "
            "(status = 'ACKNOWLEDGED' AND acknowledged_at IS NOT NULL AND attempt_count > 0)",
            name="ck_prompt_deliveries_ack_state",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dispatch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyPromptDeliveryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, delivery_id: object) -> PromptDelivery | None:
        record = self._session.get(PromptDeliveryRecord, str(delivery_id))
        return _domain_from_record(record) if record is not None else None

    def get_by_dispatch_id(self, dispatch_id: object) -> PromptDelivery | None:
        record = (
            self._session.query(PromptDeliveryRecord)
            .filter(PromptDeliveryRecord.dispatch_id == str(dispatch_id))
            .one_or_none()
        )
        return _domain_from_record(record) if record is not None else None

    def save(self, delivery: PromptDelivery) -> None:
        record = self._session.get(PromptDeliveryRecord, str(delivery.delivery_id))
        if record is None:
            self._session.add(_record_from_domain(delivery))
            return

        record.dispatch_id = str(delivery.dispatch_id)
        record.status = delivery.status.value
        record.attempt_count = delivery.attempt_count
        record.last_attempt_at = delivery.last_attempt_at
        record.acknowledged_at = delivery.acknowledged_at
        record.created_at = delivery.created_at
        record.updated_at = delivery.updated_at


def _record_from_domain(delivery: PromptDelivery) -> PromptDeliveryRecord:
    return PromptDeliveryRecord(
        delivery_id=str(delivery.delivery_id),
        dispatch_id=str(delivery.dispatch_id),
        status=delivery.status.value,
        attempt_count=delivery.attempt_count,
        last_attempt_at=delivery.last_attempt_at,
        acknowledged_at=delivery.acknowledged_at,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain_from_record(record: PromptDeliveryRecord) -> PromptDelivery:
    return PromptDelivery.rehydrate(
        delivery_id=UUID(record.delivery_id),
        dispatch_id=UUID(record.dispatch_id),
        status=record.status,
        attempt_count=record.attempt_count,
        last_attempt_at=_as_utc(record.last_attempt_at),
        acknowledged_at=_as_utc(record.acknowledged_at),
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
    )
