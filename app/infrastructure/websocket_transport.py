from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from datetime import datetime
from uuid import UUID

from app.domain.chatgpt_prompt_send import ChatGptPromptSendState

from fastapi import WebSocket

from app.application.prompt_deliveries import OutboundPromptDelivery


PROTOCOL_VERSION = 2
MAX_INBOUND_MESSAGE_BYTES = 512 * 1024
SINGLE_COMPANION_CLOSE_CODE = 4409


class ProtocolMessageError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AckMessage:
    delivery_id: UUID


@dataclass(frozen=True, slots=True)
class PingMessage:
    pass


@dataclass(frozen=True, slots=True)
class ChatGptResponseMessage:
    response_id: UUID
    delivery_id: UUID
    session: str
    text: str


@dataclass(frozen=True, slots=True)
class ChatGptSendStatusMessage:
    event_id: UUID
    delivery_id: UUID
    session: str
    state: ChatGptPromptSendState
    attempt_count: int
    conversation_id: str | None
    canonical_url: str | None
    error_code: str | None
    next_retry_at: datetime | None
    occurred_at: datetime


InboundProtocolMessage = (
    AckMessage | PingMessage | ChatGptResponseMessage | ChatGptSendStatusMessage
)


def _exact_keys(value: dict[str, object], expected: set[str]) -> bool:
    return set(value) == expected


def _uuid(value: object, *, code: str) -> UUID:
    if not isinstance(value, str):
        raise ProtocolMessageError(code)
    try:
        return UUID(value)
    except ValueError as exc:
        raise ProtocolMessageError(code) from exc


def build_prompt_message(delivery: OutboundPromptDelivery) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "prompt",
        "delivery_id": str(delivery.delivery_id),
        "payload": {
            "session": delivery.session,
            "text": delivery.text,
        },
        "routing": (
            {
                "binding_version": delivery.routing.binding_version,
                "conversation_id": delivery.routing.conversation_id,
                "canonical_url": delivery.routing.canonical_url,
            }
            if delivery.routing is not None
            else None
        ),
    }


def build_chatgpt_response_ack(response_id: UUID) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "chatgpt_response_ack",
        "response_id": str(response_id),
    }


def build_chatgpt_send_status_ack(event_id: UUID) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "chatgpt_send_status_ack",
        "event_id": str(event_id),
    }


def build_error_message(
    code: str,
    *,
    response_id: UUID | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "type": "error",
        "code": code,
    }
    if response_id is not None:
        message["response_id"] = str(response_id)
    return message


def build_pong_message() -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "pong",
    }


def parse_inbound_message(raw_message: str) -> InboundProtocolMessage:
    if len(raw_message.encode("utf-8")) > MAX_INBOUND_MESSAGE_BYTES:
        raise ProtocolMessageError("message_too_large")

    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise ProtocolMessageError("invalid_json") from exc

    if not isinstance(payload, dict):
        raise ProtocolMessageError("invalid_message")
    if payload.get("version") != PROTOCOL_VERSION:
        raise ProtocolMessageError("unsupported_version")

    message_type = payload.get("type")
    if message_type == "ack":
        if not _exact_keys(payload, {"version", "type", "delivery_id"}):
            raise ProtocolMessageError("invalid_ack")
        return AckMessage(delivery_id=_uuid(payload.get("delivery_id"), code="invalid_ack"))

    if message_type == "ping":
        if not _exact_keys(payload, {"version", "type"}):
            raise ProtocolMessageError("invalid_ping")
        return PingMessage()


    if message_type == "chatgpt_send_status":
        if not _exact_keys(
            payload,
            {"version", "type", "event_id", "delivery_id", "payload"},
        ):
            raise ProtocolMessageError("invalid_chatgpt_send_status")
        event_id = _uuid(payload.get("event_id"), code="invalid_send_event_id")
        delivery_id = _uuid(payload.get("delivery_id"), code="invalid_delivery_id")
        status_payload = payload.get("payload")
        if not isinstance(status_payload, dict) or not _exact_keys(
            status_payload,
            {
                "session",
                "state",
                "attempt",
                "conversation",
                "error_code",
                "next_retry_at",
                "occurred_at",
            },
        ):
            raise ProtocolMessageError("invalid_chatgpt_send_status_payload")
        session = status_payload.get("session")
        if not isinstance(session, str) or not session.strip():
            raise ProtocolMessageError("invalid_send_session")
        try:
            state = ChatGptPromptSendState(status_payload.get("state"))
        except (TypeError, ValueError) as exc:
            raise ProtocolMessageError("invalid_send_state") from exc
        attempt = status_payload.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise ProtocolMessageError("invalid_send_attempt")

        conversation = status_payload.get("conversation")
        conversation_id = None
        canonical_url = None
        if conversation is not None:
            if not isinstance(conversation, dict) or not _exact_keys(
                conversation,
                {"conversation_id", "canonical_url"},
            ):
                raise ProtocolMessageError("invalid_send_conversation")
            conversation_id = conversation.get("conversation_id")
            canonical_url = conversation.get("canonical_url")
            if (
                not isinstance(conversation_id, str)
                or not conversation_id.strip()
                or not isinstance(canonical_url, str)
                or not canonical_url.strip()
            ):
                raise ProtocolMessageError("invalid_send_conversation")

        error_code = status_payload.get("error_code")
        if error_code is not None and (
            not isinstance(error_code, str) or not error_code.strip()
        ):
            raise ProtocolMessageError("invalid_send_error_code")

        def _optional_datetime(value: object, code: str) -> datetime | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise ProtocolMessageError(code)
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProtocolMessageError(code) from exc
            if parsed.tzinfo is None:
                raise ProtocolMessageError(code)
            return parsed

        next_retry_at = _optional_datetime(
            status_payload.get("next_retry_at"),
            "invalid_send_next_retry_at",
        )
        occurred_at = _optional_datetime(
            status_payload.get("occurred_at"),
            "invalid_send_occurred_at",
        )
        if occurred_at is None:
            raise ProtocolMessageError("invalid_send_occurred_at")

        return ChatGptSendStatusMessage(
            event_id=event_id,
            delivery_id=delivery_id,
            session=session,
            state=state,
            attempt_count=attempt,
            conversation_id=conversation_id,
            canonical_url=canonical_url,
            error_code=error_code,
            next_retry_at=next_retry_at,
            occurred_at=occurred_at,
        )

    if message_type == "chatgpt_response":
        if not _exact_keys(
            payload,
            {"version", "type", "response_id", "delivery_id", "payload"},
        ):
            raise ProtocolMessageError("invalid_chatgpt_response")

        response_id = _uuid(payload.get("response_id"), code="invalid_response_id")
        delivery_id = _uuid(payload.get("delivery_id"), code="invalid_delivery_id")
        response_payload = payload.get("payload")
        if not isinstance(response_payload, dict) or not _exact_keys(
            response_payload,
            {"session", "text"},
        ):
            raise ProtocolMessageError("invalid_response_payload")

        session = response_payload.get("session")
        text = response_payload.get("text")
        if not isinstance(session, str) or not session.strip():
            raise ProtocolMessageError("invalid_response_session")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolMessageError("invalid_response_text")

        return ChatGptResponseMessage(
            response_id=response_id,
            delivery_id=delivery_id,
            session=session,
            text=text,
        )

    raise ProtocolMessageError("unknown_type")


class CompanionConnectionManager:
    """Own only transient socket presence; persisted delivery truth lives in SQLite."""

    def __init__(self) -> None:
        self._active: WebSocket | None = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    @property
    def has_active_connection(self) -> bool:
        return self._active is not None

    async def connect(self, websocket: WebSocket) -> bool:
        await websocket.accept()
        async with self._lock:
            if self._active is not None:
                await websocket.close(
                    code=SINGLE_COMPANION_CLOSE_CODE,
                    reason="companion_already_connected",
                )
                return False
            self._active = websocket
            return True

    async def send_json(self, payload: dict[str, object]) -> bool:
        """Send through the active companion while serializing concurrent writers."""

        async with self._send_lock:
            async with self._lock:
                websocket = self._active
            if websocket is None:
                return False
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                return False
            return True

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._active is websocket:
                self._active = None
