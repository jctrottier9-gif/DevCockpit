from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from uuid import UUID

from fastapi import WebSocket

from app.application.prompt_deliveries import OutboundPromptDelivery


PROTOCOL_VERSION = 1
MAX_INBOUND_MESSAGE_BYTES = 64 * 1024
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


InboundProtocolMessage = AckMessage | PingMessage


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


def build_error_message(code: str) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "type": "error",
        "code": code,
    }


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
        delivery_id = payload.get("delivery_id")
        if not isinstance(delivery_id, str):
            raise ProtocolMessageError("invalid_ack")
        try:
            return AckMessage(delivery_id=UUID(delivery_id))
        except ValueError as exc:
            raise ProtocolMessageError("invalid_ack") from exc

    if message_type == "ping":
        return PingMessage()

    raise ProtocolMessageError("unknown_type")


class CompanionConnectionManager:
    """Own only transient socket presence; persisted delivery truth lives in SQLite."""

    def __init__(self) -> None:
        self._active: WebSocket | None = None
        self._lock = asyncio.Lock()

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

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._active is websocket:
                self._active = None
