from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.conversation_binding import ConversationBinding
from app.infrastructure.database import Base


class ConversationBindingRecord(Base):
    __tablename__ = "conversation_bindings"
    __table_args__ = (
        UniqueConstraint("agent_session", name="uq_conversation_bindings_agent_session"),
        UniqueConstraint("conversation_id", name="uq_conversation_bindings_conversation_id"),
        UniqueConstraint("canonical_url", name="uq_conversation_bindings_canonical_url"),
        CheckConstraint(
            "state IN ('BOUND', 'INVALIDATED')",
            name="ck_conversation_bindings_state",
        ),
        CheckConstraint(
            "version >= 1 AND length(trim(agent_session)) > 0 "
            "AND length(trim(conversation_id)) > 0 AND length(trim(canonical_url)) > 0",
            name="ck_conversation_bindings_identity",
        ),
        CheckConstraint(
            "(state = 'BOUND' AND invalidated_at IS NULL AND invalidation_reason IS NULL) "
            "OR (state = 'INVALIDATED' AND invalidated_at IS NOT NULL "
            "AND invalidation_reason IS NOT NULL AND length(trim(invalidation_reason)) > 0)",
            name="ck_conversation_bindings_invalidation",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_session: Mapped[str] = mapped_column(String(450), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SqlAlchemyConversationBindingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_agent_session(self, agent_session: str) -> ConversationBinding | None:
        record = self._session.scalar(
            select(ConversationBindingRecord).where(
                ConversationBindingRecord.agent_session == agent_session
            )
        )
        return _domain_from_record(record) if record is not None else None

    def save(self, binding: ConversationBinding) -> None:
        record = self._session.get(ConversationBindingRecord, str(binding.binding_id))
        if record is None:
            self._session.add(_record_from_domain(binding))
            return
        record.agent_session = binding.agent_session
        record.conversation_id = binding.conversation_id
        record.canonical_url = binding.canonical_url
        record.state = binding.state.value
        record.version = binding.version
        record.bound_at = binding.bound_at
        record.last_validated_at = binding.last_validated_at
        record.updated_at = binding.updated_at
        record.invalidated_at = binding.invalidated_at
        record.invalidation_reason = binding.invalidation_reason


def _record_from_domain(binding: ConversationBinding) -> ConversationBindingRecord:
    return ConversationBindingRecord(
        binding_id=str(binding.binding_id),
        agent_session=binding.agent_session,
        conversation_id=binding.conversation_id,
        canonical_url=binding.canonical_url,
        state=binding.state.value,
        version=binding.version,
        bound_at=binding.bound_at,
        last_validated_at=binding.last_validated_at,
        updated_at=binding.updated_at,
        invalidated_at=binding.invalidated_at,
        invalidation_reason=binding.invalidation_reason,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain_from_record(record: ConversationBindingRecord) -> ConversationBinding:
    return ConversationBinding.rehydrate(
        binding_id=UUID(record.binding_id),
        agent_session=record.agent_session,
        conversation_id=record.conversation_id,
        canonical_url=record.canonical_url,
        state=record.state,
        version=record.version,
        bound_at=_as_utc(record.bound_at),
        last_validated_at=_as_utc(record.last_validated_at),
        updated_at=_as_utc(record.updated_at),
        invalidated_at=_as_utc(record.invalidated_at),
        invalidation_reason=record.invalidation_reason,
    )
