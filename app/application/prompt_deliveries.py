from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import AbstractSet, Callable, Protocol, Self
from uuid import UUID

from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchStatus


class PromptDispatchReadRepository(Protocol):
    def get(self, dispatch_id: object) -> PromptDispatch | None: ...

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


class PromptRedeliveryError(ValueError):
    code = "PROMPT_REDELIVERY_FAILED"


class PromptRedeliveryDispatchNotFound(PromptRedeliveryError):
    code = "PROMPT_DISPATCH_NOT_FOUND"


class PromptRedeliveryDispatchNotPrepared(PromptRedeliveryError):
    code = "PROMPT_DISPATCH_NOT_PREPARED"


class PromptRedeliveryDeliveryNotFound(PromptRedeliveryError):
    code = "PROMPT_DELIVERY_NOT_FOUND"


class PromptRedeliveryRequiresAcknowledgement(PromptRedeliveryError):
    code = "PROMPT_DELIVERY_NOT_ACKNOWLEDGED"


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


def prepare_acknowledged_prompt_redelivery(
    dispatch_id: UUID,
    *,
    uow_factory: UnitOfWorkFactory,
    now: datetime | None = None,
) -> OutboundPromptDelivery:
    """Prepare one explicit resend without erasing the original acknowledgement."""

    with uow_factory() as uow:
        dispatch = uow.prompt_dispatches.get(dispatch_id)
        if dispatch is None:
            raise PromptRedeliveryDispatchNotFound(str(dispatch_id))
        if dispatch.status is not PromptDispatchStatus.PREPARED:
            raise PromptRedeliveryDispatchNotPrepared(str(dispatch_id))

        delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
        if delivery is None:
            raise PromptRedeliveryDeliveryNotFound(str(dispatch_id))
        if not delivery.is_acknowledged:
            raise PromptRedeliveryRequiresAcknowledgement(str(dispatch_id))

        delivery.record_redelivery_attempt(now=now)
        uow.prompt_deliveries.save(delivery)
        uow.commit()

        return OutboundPromptDelivery(
            delivery_id=delivery.delivery_id,
            dispatch_id=dispatch.dispatch_id,
            session=dispatch.agent_session,
            text=dispatch.prompt_text,
            attempt_count=delivery.attempt_count,
        )


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
