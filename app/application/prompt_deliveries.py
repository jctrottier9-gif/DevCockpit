from __future__ import annotations

from collections.abc import AbstractSet
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol, Self
from uuid import UUID

from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch


class PromptDispatchReadRepository(Protocol):
    def list_prepared(self) -> list[PromptDispatch]: ...


class PromptDeliveryRepository(Protocol):
    def get(self, delivery_id: object) -> PromptDelivery | None: ...

    def get_by_dispatch_id(self, dispatch_id: object) -> PromptDelivery | None: ...

    def save(self, delivery: PromptDelivery) -> None: ...


class PromptDeliveryUnitOfWork(Protocol):
    prompt_dispatches: PromptDispatchReadRepository
    prompt_deliveries: PromptDeliveryRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], PromptDeliveryUnitOfWork]


@dataclass(frozen=True, slots=True)
class OutboundPromptDelivery:
    delivery_id: UUID
    dispatch_id: UUID
    session: str
    text: str
    attempt_count: int


class AcknowledgementResult(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DUPLICATE = "DUPLICATE"
    UNKNOWN = "UNKNOWN"


def prepare_prompt_deliveries_for_send(
    *,
    uow_factory: UnitOfWorkFactory,
    exclude_delivery_ids: AbstractSet[UUID] = frozenset(),
    now: datetime | None = None,
) -> tuple[OutboundPromptDelivery, ...]:
    """Prepare each unacknowledged PREPARED dispatch once for this connection cycle."""

    outbound: list[OutboundPromptDelivery] = []
    with uow_factory() as uow:
        for dispatch in uow.prompt_dispatches.list_prepared():
            delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
            if delivery is None:
                delivery = PromptDelivery.create(dispatch_id=dispatch.dispatch_id, now=now)

            if delivery.is_acknowledged or delivery.delivery_id in exclude_delivery_ids:
                continue

            delivery.record_attempt(now=now)
            uow.prompt_deliveries.save(delivery)
            outbound.append(
                OutboundPromptDelivery(
                    delivery_id=delivery.delivery_id,
                    dispatch_id=dispatch.dispatch_id,
                    session=dispatch.agent_session,
                    text=dispatch.prompt_text,
                    attempt_count=delivery.attempt_count,
                )
            )

        uow.commit()
    return tuple(outbound)


def acknowledge_prompt_delivery(
    delivery_id: UUID,
    *,
    uow_factory: UnitOfWorkFactory,
    now: datetime | None = None,
) -> AcknowledgementResult:
    """Record extension receipt without mutating PromptDispatch."""

    with uow_factory() as uow:
        delivery = uow.prompt_deliveries.get(delivery_id)
        if delivery is None:
            return AcknowledgementResult.UNKNOWN

        changed = delivery.acknowledge(now=now)
        if not changed:
            return AcknowledgementResult.DUPLICATE

        uow.prompt_deliveries.save(delivery)
        uow.commit()
        return AcknowledgementResult.ACKNOWLEDGED
