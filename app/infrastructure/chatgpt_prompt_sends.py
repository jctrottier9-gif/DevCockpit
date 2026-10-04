from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.chatgpt_prompt_send import (
    ChatGptPromptSend,
    ChatGptSendStatusEvent,
)
from app.infrastructure.database import Base


class ChatGptPromptSendRecord(Base):
    __tablename__ = "chatgpt_prompt_sends"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_chatgpt_prompt_sends_delivery_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('QUEUED','ROUTING','WAITING_READY','SEND_ARMED','SENT_CONFIRMED',"
            "'RETRYABLE_FAILURE','BLOCKED','AMBIGUOUS')",
            name="ck_chatgpt_prompt_sends_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_chatgpt_prompt_sends_attempt_count",
        ),
        CheckConstraint(
            "(state = 'SENT_CONFIRMED' AND confirmed_at IS NOT NULL) OR "
            "(state <> 'SENT_CONFIRMED' AND confirmed_at IS NULL)",
            name="ck_chatgpt_prompt_sends_confirmed_at",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session: Mapped[str] = mapped_column(String(450), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ChatGptSendEventRecord(Base):
    __tablename__ = "chatgpt_send_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_chatgpt_send_events_delivery_id",
            ondelete="RESTRICT",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session: Mapped[str] = mapped_column(String(450), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyChatGptPromptSendRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, delivery_id: object) -> ChatGptPromptSend | None:
        record = self._session.get(ChatGptPromptSendRecord, str(delivery_id))
        return _domain_from_record(record) if record is not None else None

    def save(self, prompt_send: ChatGptPromptSend) -> None:
        record = self._session.get(ChatGptPromptSendRecord, str(prompt_send.delivery_id))
        if record is None:
            self._session.add(_record_from_domain(prompt_send))
            return
        record.session = prompt_send.session
        record.state = prompt_send.state.value
        record.attempt_count = prompt_send.attempt_count
        record.last_error_code = prompt_send.last_error_code
        record.next_retry_at = prompt_send.next_retry_at
        record.confirmed_at = prompt_send.confirmed_at
        record.updated_at = prompt_send.updated_at
        record.last_event_id = (
            str(prompt_send.last_event_id)
            if prompt_send.last_event_id is not None
            else None
        )

    def has_event(self, event_id: object) -> bool:
        return self._session.get(ChatGptSendEventRecord, str(event_id)) is not None

    def add_event(self, event: ChatGptSendStatusEvent) -> None:
        self._session.add(
            ChatGptSendEventRecord(
                event_id=str(event.event_id),
                delivery_id=str(event.delivery_id),
                session=event.session,
                state=event.state.value,
                attempt_count=event.attempt_count,
                conversation_id=event.conversation_id,
                canonical_url=event.canonical_url,
                error_code=event.error_code,
                next_retry_at=event.next_retry_at,
                occurred_at=event.occurred_at,
                created_at=datetime.now(timezone.utc),
            )
        )


def _record_from_domain(prompt_send: ChatGptPromptSend) -> ChatGptPromptSendRecord:
    return ChatGptPromptSendRecord(
        delivery_id=str(prompt_send.delivery_id),
        session=prompt_send.session,
        state=prompt_send.state.value,
        attempt_count=prompt_send.attempt_count,
        last_error_code=prompt_send.last_error_code,
        next_retry_at=prompt_send.next_retry_at,
        confirmed_at=prompt_send.confirmed_at,
        updated_at=prompt_send.updated_at,
        last_event_id=(
            str(prompt_send.last_event_id)
            if prompt_send.last_event_id is not None
            else None
        ),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain_from_record(record: ChatGptPromptSendRecord) -> ChatGptPromptSend:
    return ChatGptPromptSend.rehydrate(
        delivery_id=UUID(record.delivery_id),
        session=record.session,
        state=record.state,
        attempt_count=record.attempt_count,
        last_error_code=record.last_error_code,
        next_retry_at=_as_utc(record.next_retry_at),
        confirmed_at=_as_utc(record.confirmed_at),
        updated_at=_as_utc(record.updated_at),
        last_event_id=UUID(record.last_event_id) if record.last_event_id else None,
    )
