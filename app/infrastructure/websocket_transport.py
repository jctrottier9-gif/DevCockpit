from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from uuid import UUID

from fastapi import WebSocket

from app.application.prompt_deliveries import OutboundPromptDelivery


PROTOCOL_VERSION = 1
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


InboundProtocolMessage = AckMessage | PingMessage | ChatGptResponseMessage


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
    }


def build_chatgpt_response_ack(response_id: UUID) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "chatgpt_response_ack",
        "response_id": str(response_id),
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
