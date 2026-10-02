from dataclasses import asdict, fields
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.handoff import OrchestrationConflict
from app.domain.roadmap_change import ProposalStatus, RoadmapChangeProposal, RoadmapChangeProposalRevision
from app.infrastructure.database import Base


class RoadmapChangeProposalRecord(Base):
    __tablename__ = "roadmap_change_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','APPLIED','CANCELLED')",
            name="ck_roadmap_proposal_status",
        ),
        CheckConstraint("version >= 1 AND current_revision >= 1", name="ck_roadmap_proposal_version"),
        CheckConstraint(
            "roadmap_issue_number >= 1 AND length(trim(project_id)) > 0 "
            "AND length(trim(repository_full_name)) > 0 AND length(trim(created_by)) > 0",
            name="ck_roadmap_proposal_identity",
        ),
        CheckConstraint(
            "(status = 'CANCELLED' AND cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL "
            "AND cancellation_command_id IS NOT NULL AND length(trim(cancelled_by)) > 0) "
            "OR (status != 'CANCELLED' AND cancelled_by IS NULL AND cancelled_at IS NULL "
            "AND cancellation_command_id IS NULL)",
            name="ck_roadmap_proposal_cancel",
        ),
    )
    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_full_name: Mapped[str] = mapped_column(String(300), nullable=False)
    roadmap_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_decision_id: Mapped[str] = mapped_column(
        ForeignKey("decisions.decision_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    creation_command_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_command_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    confirmed_revision: Mapped[int | None] = mapped_column(Integer)
    confirmed_preview_digest: Mapped[str | None] = mapped_column(String(64))
    confirmation_command_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    writeback_authorization_decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("decisions.decision_id", ondelete="RESTRICT")
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoadmapChangeProposalRevisionRecord(Base):
    __tablename__ = "roadmap_change_proposal_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_roadmap_proposal_revision_number"),
        CheckConstraint(
            "length(base_body_hash) = 64 AND length(proposed_body_hash) = 64",
            name="ck_roadmap_proposal_revision_hashes",
        ),
        UniqueConstraint("revision_command_id", name="uq_roadmap_proposal_revision_command"),
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("roadmap_change_proposals.proposal_id", ondelete="RESTRICT"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_body: Mapped[str] = mapped_column(Text, nullable=False)
    base_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_updated_at: Mapped[str | None] = mapped_column(Text)
    proposed_body: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operations_json: Mapped[str] = mapped_column(Text, nullable=False)
    generator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    validation_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_command_id: Mapped[str] = mapped_column(String(36), nullable=False)


_UUID_FIELDS = {
    "proposal_id", "source_decision_id", "creation_command_id",
    "cancellation_command_id", "revision_command_id",
    "confirmation_command_id", "writeback_authorization_decision_id",
}


def record_values(domain):
    result = {}
    for key, value in asdict(domain).items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, ProposalStatus):
            result[key] = value.value
        else:
            result[key] = value
    return result


def domain_entity(record, cls):
    if record is None:
        return None
    data = {field.name: getattr(record, field.name) for field in fields(cls)}
    for key, value in data.items():
        if key in _UUID_FIELDS and value is not None:
            data[key] = UUID(value)
        elif isinstance(value, datetime) and value.tzinfo is None:
            data[key] = value.replace(tzinfo=timezone.utc)
    if cls is RoadmapChangeProposal:
        data["status"] = ProposalStatus(data["status"])
    return cls(**data)


class SqlAlchemyRoadmapChangeProposalRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, identity):
        return domain_entity(
            self.session.get(RoadmapChangeProposalRecord, str(identity)),
            RoadmapChangeProposal,
        )

    def by_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeProposalRecord).where(
                RoadmapChangeProposalRecord.creation_command_id == str(identity)
            )
        )
        return domain_entity(record, RoadmapChangeProposal)

    def by_cancellation_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeProposalRecord).where(
                RoadmapChangeProposalRecord.cancellation_command_id == str(identity)
            )
        )
        return domain_entity(record, RoadmapChangeProposal)

    def by_confirmation_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeProposalRecord).where(
                RoadmapChangeProposalRecord.confirmation_command_id == str(identity)
            )
        )
        return domain_entity(record, RoadmapChangeProposal)

    def list_for_decision(self, decision_id):
        records = self.session.scalars(
            select(RoadmapChangeProposalRecord)
            .where(RoadmapChangeProposalRecord.source_decision_id == str(decision_id))
            .order_by(RoadmapChangeProposalRecord.created_at, RoadmapChangeProposalRecord.proposal_id)
        )
        return [domain_entity(record, RoadmapChangeProposal) for record in records]

    def add(self, proposal):
        self.session.add(RoadmapChangeProposalRecord(**record_values(proposal)))

    def save(self, proposal, expected_version):
        record = self.session.get(RoadmapChangeProposalRecord, str(proposal.proposal_id))
        if record is None or record.version != expected_version:
            raise OrchestrationConflict("RoadmapChangeProposal was changed by another command")
        for key, value in record_values(proposal).items():
            setattr(record, key, value)


class SqlAlchemyRoadmapChangeProposalRevisionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, proposal_id, revision):
        record = self.session.get(
            RoadmapChangeProposalRevisionRecord,
            (str(proposal_id), int(revision)),
        )
        return domain_entity(record, RoadmapChangeProposalRevision)

    def by_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeProposalRevisionRecord).where(
                RoadmapChangeProposalRevisionRecord.revision_command_id == str(identity)
            )
        )
        return domain_entity(record, RoadmapChangeProposalRevision)

    def list_for_proposal(self, proposal_id):
        records = self.session.scalars(
            select(RoadmapChangeProposalRevisionRecord)
            .where(RoadmapChangeProposalRevisionRecord.proposal_id == str(proposal_id))
            .order_by(RoadmapChangeProposalRevisionRecord.revision)
        )
        return [domain_entity(record, RoadmapChangeProposalRevision) for record in records]

    def add(self, revision):
        self.session.add(RoadmapChangeProposalRevisionRecord(**record_values(revision)))
