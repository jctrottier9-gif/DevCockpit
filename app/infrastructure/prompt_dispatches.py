from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, Text, UniqueConstraint, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.prompt_dispatch import PromptDispatch
from app.infrastructure.chatgpt_responses import SqlAlchemyImportedChatGptResponseRepository
from app.infrastructure.database import Base
from app.infrastructure.handoffs import SqlAlchemyDecisionRepository, SqlAlchemyHandoffRepository
from app.infrastructure.prompt_deliveries import SqlAlchemyPromptDeliveryRepository
from app.infrastructure.roadmap_changes import (
    SqlAlchemyRoadmapChangeProposalRepository,
    SqlAlchemyRoadmapChangeProposalRevisionRepository,
)


class PromptDispatchRecord(Base):
    __tablename__ = "prompt_dispatches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_prompt_dispatches_idempotency_key"),
        CheckConstraint("role IN ('PO', 'ARCH', 'DEV')", name="ck_prompt_dispatches_role"),
        CheckConstraint(
            "status IN ('PREPARED', 'CANCELLED')",
            name="ck_prompt_dispatches_status",
        ),
    )

    dispatch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_session: Mapped[str] = mapped_column(String(450), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyPromptDispatchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, dispatch: PromptDispatch) -> None:
        self._session.add(_record_from_domain(dispatch))

    def save(self, dispatch: PromptDispatch) -> None:
        record = self._session.get(PromptDispatchRecord, str(dispatch.dispatch_id))
        if record is None:
            self.add(dispatch)
            return
        record.project_id = dispatch.project_id
        record.work_item_id = dispatch.work_item_id
        record.role = dispatch.role.value
        record.agent_session = dispatch.agent_session
        record.prompt_text = dispatch.prompt_text
        record.status = dispatch.status.value
        record.idempotency_key = dispatch.idempotency_key
        record.created_at = dispatch.created_at
        record.updated_at = dispatch.updated_at

    def get(self, dispatch_id: object) -> PromptDispatch | None:
        record = self._session.get(PromptDispatchRecord, str(dispatch_id))
        return _domain_from_record(record) if record is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> PromptDispatch | None:
        record = self._session.scalar(
            select(PromptDispatchRecord).where(
                PromptDispatchRecord.idempotency_key == idempotency_key
            )
        )
        return _domain_from_record(record) if record is not None else None

    def list_for_work_item(self, project_id, work_item_id):
        records = self._session.scalars(
            select(PromptDispatchRecord)
            .where(
                PromptDispatchRecord.project_id == project_id,
                PromptDispatchRecord.work_item_id == work_item_id,
            )
            .order_by(PromptDispatchRecord.created_at, PromptDispatchRecord.dispatch_id)
        )
        return [_domain_from_record(record) for record in records]

    def list_prepared(self) -> list[PromptDispatch]:
        records = self._session.scalars(
            select(PromptDispatchRecord)
            .where(PromptDispatchRecord.status == "PREPARED")
            .order_by(PromptDispatchRecord.created_at, PromptDispatchRecord.dispatch_id)
        ).all()
        return [_domain_from_record(record) for record in records]


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.prompt_dispatches: SqlAlchemyPromptDispatchRepository
        self.prompt_deliveries: SqlAlchemyPromptDeliveryRepository
        self.chatgpt_responses: SqlAlchemyImportedChatGptResponseRepository
        self.handoffs: SqlAlchemyHandoffRepository
        self.decisions: SqlAlchemyDecisionRepository
        self.roadmap_change_proposals: SqlAlchemyRoadmapChangeProposalRepository
        self.roadmap_change_proposal_revisions: SqlAlchemyRoadmapChangeProposalRevisionRepository

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        # Reserve SQLite's writer before reading policy. Handoff transfer,
        # proposal revision and the execution poller therefore share one writer
        # boundary and cannot expose an "unblocked" gap.
        if self._session.get_bind().dialect.name == "sqlite":
            try:
                self._session.execute(text("BEGIN IMMEDIATE"))
            except Exception:
                self._session.close()
                self._session = None
                raise
        self.handoffs = SqlAlchemyHandoffRepository(self._session)
        self.decisions = SqlAlchemyDecisionRepository(self._session)
        self.roadmap_change_proposals = SqlAlchemyRoadmapChangeProposalRepository(self._session)
        self.roadmap_change_proposal_revisions = SqlAlchemyRoadmapChangeProposalRevisionRepository(self._session)
        self.prompt_dispatches = SqlAlchemyPromptDispatchRepository(self._session)
        self.prompt_deliveries = SqlAlchemyPromptDeliveryRepository(self._session)
        self.chatgpt_responses = SqlAlchemyImportedChatGptResponseRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            self._session.rollback()
        self._session.close()
        self._session = None

    def commit(self) -> None:
        self._require_session().commit()

    def flush(self) -> None:
        self._require_session().flush()

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("Unit of Work is not active")
        return self._session


def _record_from_domain(dispatch: PromptDispatch) -> PromptDispatchRecord:
    return PromptDispatchRecord(
        dispatch_id=str(dispatch.dispatch_id),
        project_id=dispatch.project_id,
        work_item_id=dispatch.work_item_id,
        role=dispatch.role.value,
        agent_session=dispatch.agent_session,
        prompt_text=dispatch.prompt_text,
        status=dispatch.status.value,
        idempotency_key=dispatch.idempotency_key,
        created_at=dispatch.created_at,
        updated_at=dispatch.updated_at,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain_from_record(record: PromptDispatchRecord) -> PromptDispatch:
    return PromptDispatch.rehydrate(
        dispatch_id=UUID(record.dispatch_id),
        project_id=record.project_id,
        work_item_id=record.work_item_id,
        role=record.role,
        agent_session=record.agent_session,
        prompt_text=record.prompt_text,
        status=record.status,
        idempotency_key=record.idempotency_key,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
    )
