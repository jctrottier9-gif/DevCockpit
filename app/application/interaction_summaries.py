from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole


PROMPT_PREPARED = "PROMPT_PREPARED"
PROMPT_CANCELLED = "PROMPT_CANCELLED"
FIREFOX_ACKNOWLEDGED = "FIREFOX_ACKNOWLEDGED"
RESPONSE_IMPORTED = "RESPONSE_IMPORTED"


@dataclass(frozen=True, slots=True)
class InteractionSummary:
    """Read-only projection of one prompt across dispatch, Firefox, ChatGPT and response import."""

    project_id: str
    work_item_id: str
    role: str
    agent_session: str
    dispatch_id: str
    dispatch_status: str
    delivery_id: str | None
    delivery_status: str | None
    delivery_acknowledged_at: datetime | None
    state: str
    send_state: str | None
    send_attempt_count: int | None
    send_error_code: str | None
    send_confirmed_at: datetime | None
    imported_response_available: bool
    imported_response_count: int
    latest_imported_response_at: datetime | None
    manual_send_required: bool
    automatic_resend_allowed: bool


def read_interaction_summary(dispatch: PromptDispatch, *, uow) -> InteractionSummary:
    delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
    send_repository = getattr(uow, "chatgpt_prompt_sends", None)
    prompt_send = (
        send_repository.get(delivery.delivery_id)
        if send_repository is not None and delivery is not None
        else None
    )

    response_repository = getattr(uow, "chatgpt_responses", None)
    responses = []
    if response_repository is not None and delivery is not None:
        responses = [
            response
            for response in response_repository.list_all()
            if response.delivery_id == delivery.delivery_id
        ]

    if responses:
        state = RESPONSE_IMPORTED
    elif prompt_send is not None:
        state = prompt_send.state.value
    elif delivery is not None and delivery.is_acknowledged:
        state = FIREFOX_ACKNOWLEDGED
    elif dispatch.status.value == "CANCELLED":
        state = PROMPT_CANCELLED
    else:
        state = PROMPT_PREPARED

    manual_send_required = (
        dispatch.role is PromptDispatchRole.ARCH
        and not responses
        and state
        not in {
            "ROUTING",
            "WAITING_READY",
            "SEND_ARMED",
            "SENT_CONFIRMED",
            "AMBIGUOUS",
        }
        and delivery is not None
        and delivery.is_acknowledged
    )
    automatic_resend_allowed = state not in {
        "SEND_ARMED",
        "SENT_CONFIRMED",
        "AMBIGUOUS",
        RESPONSE_IMPORTED,
    }

    latest_response = max(
        (response.imported_at for response in responses),
        default=None,
    )

    return InteractionSummary(
        project_id=dispatch.project_id,
        work_item_id=dispatch.work_item_id,
        role=dispatch.role.value,
        agent_session=dispatch.agent_session,
        dispatch_id=str(dispatch.dispatch_id),
        dispatch_status=dispatch.status.value,
        delivery_id=str(delivery.delivery_id) if delivery is not None else None,
        delivery_status=delivery.status.value if delivery is not None else None,
        delivery_acknowledged_at=(
            delivery.acknowledged_at if delivery is not None else None
        ),
        state=state,
        send_state=prompt_send.state.value if prompt_send is not None else None,
        send_attempt_count=(
            prompt_send.attempt_count if prompt_send is not None else None
        ),
        send_error_code=(
            prompt_send.last_error_code if prompt_send is not None else None
        ),
        send_confirmed_at=(
            prompt_send.confirmed_at if prompt_send is not None else None
        ),
        imported_response_available=bool(responses),
        imported_response_count=len(responses),
        latest_imported_response_at=latest_response,
        manual_send_required=manual_send_required,
        automatic_resend_allowed=automatic_resend_allowed,
    )


def interaction_indication(summary: InteractionSummary) -> str:
    if summary.state == RESPONSE_IMPORTED:
        return "Réponse importée à examiner"
    if summary.state == "AMBIGUOUS":
        return "Envoi ambigu · vérifier avant toute reprise"
    if summary.state == "BLOCKED":
        return "Envoi ChatGPT bloqué"
    if summary.manual_send_required:
        return "Gate ARCH prête · lancement manuel dans Firefox requis"
    if summary.state == "SENT_CONFIRMED":
        return "Envoi ChatGPT confirmé · aucune réponse importée"
    if summary.state == "SEND_ARMED":
        return "Envoi armé · confirmation ciblée en attente"
    if summary.state == "WAITING_READY":
        return "ChatGPT pas encore prêt"
    if summary.state == "ROUTING":
        return "Routage ChatGPT en cours"
    if summary.state == "RETRYABLE_FAILURE":
        return "Échec certain avant envoi · reprise bornée"
    if summary.state == "QUEUED":
        return "En file companion"
    if summary.state == FIREFOX_ACKNOWLEDGED:
        return "Reçu et persisté par Firefox"
    if summary.state == PROMPT_CANCELLED:
        return "Prompt annulé"
    return "Prompt préparé"
