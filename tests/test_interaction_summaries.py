from datetime import datetime, timezone
from types import SimpleNamespace

from app.application.interaction_summaries import (
    FIREFOX_ACKNOWLEDGED,
    PROMPT_PREPARED,
    RESPONSE_IMPORTED,
    interaction_indication,
    read_interaction_summary,
)
from app.domain.chatgpt_prompt_send import ChatGptPromptSend, ChatGptPromptSendState
from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole


NOW = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)


class Deliveries:
    def __init__(self, delivery=None):
        self.delivery = delivery

    def get_by_dispatch_id(self, dispatch_id):
        if self.delivery is not None and self.delivery.dispatch_id == dispatch_id:
            return self.delivery
        return None


class PromptSends:
    def __init__(self, prompt_send=None):
        self.prompt_send = prompt_send

    def get(self, delivery_id):
        if self.prompt_send is not None and self.prompt_send.delivery_id == delivery_id:
            return self.prompt_send
        return None


class Responses:
    def __init__(self, responses=()):
        self.responses = list(responses)

    def list_all(self):
        return list(self.responses)


class Uow:
    def __init__(self, *, delivery=None, prompt_send=None, responses=()):
        self.prompt_deliveries = Deliveries(delivery)
        self.chatgpt_prompt_sends = PromptSends(prompt_send)
        self.chatgpt_responses = Responses(responses)


def dispatch(role=PromptDispatchRole.DEV):
    return PromptDispatch.prepare(
        project_id="DevCockpit",
        work_item_id="DC-070E",
        role=role,
        prompt_text="test",
        idempotency_key=f"test:{role.value}",
        now=NOW,
    )


def acknowledged_delivery(source):
    delivery = PromptDelivery.create(dispatch_id=source.dispatch_id, now=NOW)
    delivery.record_attempt(now=NOW)
    delivery.acknowledge(now=NOW)
    return delivery


def prompt_send(delivery, source, state, *, error=None):
    confirmed_at = NOW if state is ChatGptPromptSendState.SENT_CONFIRMED else None
    return ChatGptPromptSend.rehydrate(
        delivery_id=delivery.delivery_id,
        session=source.agent_session,
        state=state,
        attempt_count=1,
        last_error_code=error,
        next_retry_at=None,
        confirmed_at=confirmed_at,
        updated_at=NOW,
        last_event_id=None,
    )


def test_interaction_summary_preserves_dispatch_and_firefox_boundaries():
    source = dispatch()

    prepared = read_interaction_summary(source, uow=Uow())
    assert prepared.state == PROMPT_PREPARED
    assert prepared.delivery_id is None
    assert prepared.agent_session == "DevCockpit:DEV:DC-070E"

    delivery = acknowledged_delivery(source)
    acknowledged = read_interaction_summary(source, uow=Uow(delivery=delivery))
    assert acknowledged.state == FIREFOX_ACKNOWLEDGED
    assert acknowledged.delivery_id == str(delivery.delivery_id)
    assert acknowledged.delivery_status == "ACKNOWLEDGED"


def test_sent_confirmed_does_not_claim_response_or_completion():
    source = dispatch()
    delivery = acknowledged_delivery(source)
    send = prompt_send(delivery, source, ChatGptPromptSendState.SENT_CONFIRMED)

    summary = read_interaction_summary(
        source,
        uow=Uow(delivery=delivery, prompt_send=send),
    )

    assert summary.state == "SENT_CONFIRMED"
    assert summary.send_state == "SENT_CONFIRMED"
    assert summary.imported_response_available is False
    assert interaction_indication(summary) == (
        "Envoi ChatGPT confirmé · aucune réponse importée"
    )


def test_imported_response_is_the_only_response_available_fact():
    source = dispatch()
    delivery = acknowledged_delivery(source)
    send = prompt_send(delivery, source, ChatGptPromptSendState.SENT_CONFIRMED)
    response = SimpleNamespace(
        delivery_id=delivery.delivery_id,
        imported_at=NOW,
    )

    summary = read_interaction_summary(
        source,
        uow=Uow(delivery=delivery, prompt_send=send, responses=(response,)),
    )

    assert summary.state == RESPONSE_IMPORTED
    assert summary.imported_response_available
    assert summary.imported_response_count == 1
    assert summary.latest_imported_response_at == NOW


def test_arch_queued_requires_manual_launch_and_ambiguous_forbids_resend():
    source = dispatch(PromptDispatchRole.ARCH)
    delivery = acknowledged_delivery(source)
    queued = prompt_send(delivery, source, ChatGptPromptSendState.QUEUED)

    manual = read_interaction_summary(
        source,
        uow=Uow(delivery=delivery, prompt_send=queued),
    )
    assert manual.manual_send_required
    assert manual.automatic_resend_allowed is False
    assert interaction_indication(manual) == (
        "Gate ARCH prête · lancement manuel dans Firefox requis"
    )

    ambiguous = prompt_send(delivery, source, ChatGptPromptSendState.AMBIGUOUS)
    uncertain = read_interaction_summary(
        source,
        uow=Uow(delivery=delivery, prompt_send=ambiguous),
    )
    assert uncertain.state == "AMBIGUOUS"
    assert uncertain.manual_send_required is False
    assert uncertain.automatic_resend_allowed is False
    assert interaction_indication(uncertain) == (
        "Envoi ambigu · vérifier avant toute reprise"
    )
