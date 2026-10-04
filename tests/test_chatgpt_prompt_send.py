from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.domain.chatgpt_prompt_send import (
    ChatGptPromptSend,
    ChatGptPromptSendState,
    ChatGptSendStatusEvent,
)


DELIVERY_ID = UUID("8fcd3422-3dbd-481f-a5b0-5915a1f7f5be")
SESSION = "DevCockpit:DEV:DC-063B"


def _event(
    suffix: int,
    *,
    state: ChatGptPromptSendState,
    attempt: int,
    occurred_at: datetime,
    conversation: bool = False,
) -> ChatGptSendStatusEvent:
    return ChatGptSendStatusEvent(
        event_id=UUID(f"00000000-0000-4000-8000-{suffix:012d}"),
        delivery_id=DELIVERY_ID,
        session=SESSION,
        state=state,
        attempt_count=attempt,
        conversation_id="conversation-063b" if conversation else None,
        canonical_url=(
            "https://chatgpt.com/c/conversation-063b"
            if conversation
            else None
        ),
        error_code=None,
        next_retry_at=None,
        occurred_at=occurred_at,
    )


def test_send_confirmation_uses_backend_clock_and_never_regresses_after_armed() -> None:
    browser_time = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    backend_time = browser_time + timedelta(hours=3)
    prompt_send = ChatGptPromptSend.queued(
        delivery_id=DELIVERY_ID,
        session=SESSION,
        now=backend_time,
    )

    assert prompt_send.apply_event(
        _event(
            1,
            state=ChatGptPromptSendState.ROUTING,
            attempt=1,
            occurred_at=browser_time,
        ),
        recorded_at=backend_time + timedelta(seconds=1),
    )
    assert prompt_send.apply_event(
        _event(
            2,
            state=ChatGptPromptSendState.SEND_ARMED,
            attempt=1,
            occurred_at=browser_time + timedelta(seconds=1),
        ),
        recorded_at=backend_time + timedelta(seconds=2),
    )
    confirmed_at = backend_time + timedelta(seconds=3)
    assert prompt_send.apply_event(
        _event(
            3,
            state=ChatGptPromptSendState.SENT_CONFIRMED,
            attempt=1,
            occurred_at=browser_time + timedelta(seconds=2),
            conversation=True,
        ),
        recorded_at=confirmed_at,
    )

    assert prompt_send.state is ChatGptPromptSendState.SENT_CONFIRMED
    assert prompt_send.confirmed_at == confirmed_at
    assert (
        prompt_send.apply_event(
            _event(
                4,
                state=ChatGptPromptSendState.ROUTING,
                attempt=2,
                occurred_at=browser_time + timedelta(seconds=3),
            ),
            recorded_at=backend_time + timedelta(seconds=4),
        )
        is False
    )


def test_blocked_pre_barrier_send_can_resume_only_on_new_attempt() -> None:
    now = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
    prompt_send = ChatGptPromptSend.queued(
        delivery_id=DELIVERY_ID,
        session=SESSION,
        now=now,
    )
    assert prompt_send.apply_event(
        _event(
            1,
            state=ChatGptPromptSendState.BLOCKED,
            attempt=1,
            occurred_at=now,
        ),
        recorded_at=now + timedelta(seconds=1),
    )
    assert prompt_send.state is ChatGptPromptSendState.BLOCKED
    assert prompt_send.apply_event(
        _event(
            2,
            state=ChatGptPromptSendState.ROUTING,
            attempt=2,
            occurred_at=now + timedelta(seconds=2),
        ),
        recorded_at=now + timedelta(seconds=2),
    )
    assert prompt_send.state is ChatGptPromptSendState.ROUTING
