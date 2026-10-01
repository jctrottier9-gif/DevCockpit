from dataclasses import asdict, fields
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import (CheckConstraint, DateTime, ForeignKey, Index, Integer,
                        String, Text, select, update, text)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.handoff import Decision, DecisionEffect, Handoff, HandoffStatus, OrchestrationConflict
from app.infrastructure.database import Base


class HandoffRecord(Base):
    __tablename__ = 'handoffs'
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','DECIDED','RESUME_PREPARED','CANCELLED')", name='ck_handoff_status'),
        CheckConstraint("target_role = 'ARCH' AND purpose = 'TECHNICAL_GUIDANCE'", name='ck_handoff_role'),
        CheckConstraint('version >= 1', name='ck_handoff_version'),
        CheckConstraint('length(trim(question)) > 0 AND length(trim(context)) > 0 AND length(trim(context_snapshot)) > 0 AND length(trim(created_by)) > 0', name='ck_handoff_text'),
        CheckConstraint("(status = 'RESUME_PREPARED' AND resume_dispatch_id IS NOT NULL) OR (status != 'RESUME_PREPARED' AND resume_dispatch_id IS NULL)", name='ck_handoff_resume'),
        CheckConstraint("(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancel_reason IS NOT NULL AND length(trim(cancelled_by)) > 0 AND length(trim(cancel_reason)) > 0 AND cancellation_command_id IS NOT NULL) OR (status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancel_reason IS NULL AND cancellation_command_id IS NULL)", name='ck_handoff_cancel'),
        Index('ix_handoffs_project_work_status', 'project_id', 'work_item_id', 'status'),
        Index('uq_handoffs_active', 'project_id', 'work_item_id', unique=True,
              sqlite_where=text("status IN ('OPEN','DECIDED')")),
    )
    handoff_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200))
    work_item_id: Mapped[str] = mapped_column(String(200))
    source_dispatch_id: Mapped[str] = mapped_column(ForeignKey('prompt_dispatches.dispatch_id', ondelete='RESTRICT'))
    source_response_id: Mapped[str | None] = mapped_column(ForeignKey('imported_chatgpt_responses.response_id', ondelete='RESTRICT'))
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text)
    context_snapshot: Mapped[str] = mapped_column(Text)
    request_dispatch_id: Mapped[str] = mapped_column(ForeignKey('prompt_dispatches.dispatch_id', ondelete='RESTRICT'), unique=True)
    resume_dispatch_id: Mapped[str | None] = mapped_column(ForeignKey('prompt_dispatches.dispatch_id', ondelete='RESTRICT'), unique=True)
    creation_command_id: Mapped[str] = mapped_column(String(36), unique=True)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_role: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    covered_github_evidence: Mapped[str | None] = mapped_column(Text)
    resume_held_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancellation_command_id: Mapped[str | None] = mapped_column(String(36), unique=True)


class DecisionRecord(Base):
    __tablename__ = 'decisions'
    __table_args__ = (
        CheckConstraint("effect IN ('CONTINUE_IN_SCOPE','HOLD_FOR_AUTHORIZATION')", name='ck_decision_effect'),
        CheckConstraint("decision_type = 'ARCHITECTURE_GUIDANCE'", name='ck_decision_type'),
        CheckConstraint('length(trim(summary)) > 0 AND length(trim(accepted_by)) > 0', name='ck_decision_text'),
    )
    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_handoff_id: Mapped[str] = mapped_column(ForeignKey('handoffs.handoff_id', ondelete='RESTRICT'), unique=True)
    source_response_id: Mapped[str] = mapped_column(ForeignKey('imported_chatgpt_responses.response_id', ondelete='RESTRICT'))
    summary: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(String(32))
    accepted_by: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acceptance_command_id: Mapped[str] = mapped_column(String(36), unique=True)
    decision_type: Mapped[str] = mapped_column(String(32))


_UUID_FIELDS = {'handoff_id','source_dispatch_id','source_response_id','request_dispatch_id',
                'resume_dispatch_id','creation_command_id','cancellation_command_id',
                'decision_id','source_handoff_id','acceptance_command_id'}


def values(entity):
    return {key: str(value) if isinstance(value, UUID) else value for key, value in asdict(entity).items()}


def entity(record, cls):
    if record is None:
        return None
    data = {f.name: getattr(record, f.name) for f in fields(cls)}
    for key, value in data.items():
        if key in _UUID_FIELDS and value is not None:
            data[key] = UUID(value)
        elif isinstance(value, datetime) and value.tzinfo is None:
            data[key] = value.replace(tzinfo=timezone.utc)
    if cls is Handoff:
        data['status'] = HandoffStatus(data['status'])
    else:
        data['effect'] = DecisionEffect(data['effect'])
    return cls(**data)


class SqlAlchemyHandoffRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, identity):
        return entity(self.session.get(HandoffRecord, str(identity)), Handoff)

    def by_command(self, identity):
        return entity(self.session.scalar(select(HandoffRecord).where(HandoffRecord.creation_command_id == str(identity))), Handoff)

    def by_cancellation_command(self, identity):
        return entity(self.session.scalar(select(HandoffRecord).where(HandoffRecord.cancellation_command_id == str(identity))), Handoff)

    def list_for_work_item(self, project_id, work_item_id):
        records = self.session.scalars(select(HandoffRecord).where(
            HandoffRecord.project_id == project_id, HandoffRecord.work_item_id == work_item_id
        ).order_by(HandoffRecord.created_at, HandoffRecord.handoff_id))
        return [entity(r, Handoff) for r in records]

    def active(self, project_id, work_item_id):
        return next((h for h in self.list_for_work_item(project_id, work_item_id) if h.blocking), None)

    def covers(self, project_id, work_item_id, evidence_key):
        return any(h.covered_github_evidence == evidence_key for h in self.list_for_work_item(project_id, work_item_id))

    def add(self, handoff):
        self.session.add(HandoffRecord(**values(handoff)))

    def save(self, handoff, expected_version):
        result = self.session.execute(update(HandoffRecord).where(
            HandoffRecord.handoff_id == str(handoff.handoff_id),
            HandoffRecord.version == expected_version,
        ).values(**values(handoff)))
        if result.rowcount != 1:
            raise OrchestrationConflict('Handoff was changed by another command')


class SqlAlchemyDecisionRepository:
    def __init__(self, session: Session):
        self.session = session

    def by_command(self, identity):
        return entity(self.session.scalar(select(DecisionRecord).where(DecisionRecord.acceptance_command_id == str(identity))), Decision)

    def for_handoff(self, identity):
        return entity(self.session.scalar(select(DecisionRecord).where(DecisionRecord.source_handoff_id == str(identity))), Decision)

    def add(self, decision):
        self.session.add(DecisionRecord(**values(decision)))
