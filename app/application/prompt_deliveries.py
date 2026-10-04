from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import AbstractSet, Callable, Protocol, Self
from uuid import UUID

from app.application.conversation_bindings import ConversationRoutingSnapshot
from app.domain.conversation_binding import ConversationBinding, ConversationBindingState
from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchStatus


class PromptDispatchReadRepository(Protocol):
    def get(self, dispatch_id: object) -> PromptDispatch | None: ...

    def list_prepared(self) -> list[PromptDispatch]: ...


class PromptDeliveryRepository(Protocol):
    def get(self, delivery_id: object) -> PromptDelivery | None: ...

    def get_by_dispatch_id(self, dispatch_id: object) -> PromptDelivery | None: ...

    def save(self, delivery: PromptDelivery) -> None: ...


class ConversationBindingReadRepository(Protocol):
    def get_by_agent_session(self, agent_session: str) -> ConversationBinding | None: ...


class PromptDeliveryUnitOfWork(Protocol):
    prompt_dispatches: PromptDispatchReadRepository
    prompt_deliveries: PromptDeliveryRepository
    conversation_bindings: ConversationBindingReadRepository

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
    routing: ConversationRoutingSnapshot | None


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


class PromptRedeliveryBindingInvalidated(PromptRedeliveryError):
    code = "CONVERSATION_BINDING_INVALIDATED"


def _routing_snapshot(binding: ConversationBinding | None) -> ConversationRoutingSnapshot | None:
    if binding is None:
        return None
    if binding.state is ConversationBindingState.INVALIDATED:
        return None
    return ConversationRoutingSnapshot(
        binding_version=binding.version,
        conversation_id=binding.conversation_id,
        canonical_url=binding.canonical_url,
    )


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
            binding = uow.conversation_bindings.get_by_agent_session(dispatch.agent_session)
            if binding is not None and binding.state is ConversationBindingState.INVALIDATED:
                continue

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
                    routing=_routing_snapshot(binding),
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

        binding = uow.conversation_bindings.get_by_agent_session(dispatch.agent_session)
        if binding is not None and binding.state is ConversationBindingState.INVALIDATED:
            raise PromptRedeliveryBindingInvalidated(dispatch.agent_session)

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
            routing=_routing_snapshot(binding),
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
        from app.application.chatgpt_prompt_sends import ensure_chatgpt_prompt_send_for_ack

        ensure_chatgpt_prompt_send_for_ack(
            delivery.delivery_id,
            uow=uow,
            now=delivery.acknowledged_at,
        )
        uow.commit()
        return AcknowledgementResult.ACKNOWLEDGED
