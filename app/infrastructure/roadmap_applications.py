from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.handoff import OrchestrationConflict
from app.domain.roadmap_change import (
    ApplicationAttemptOutcome,
    ApplicationStatus,
    RoadmapChangeApplication,
    RoadmapChangeApplicationAttempt,
)
from app.infrastructure.database import Base


class RoadmapChangeApplicationRecord(Base):
    __tablename__ = "roadmap_change_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PREPARED','APPLYING','APPLIED','NOT_APPLIED','CONFLICT','RECONCILIATION_REQUIRED')",
            name="ck_roadmap_application_status",
        ),
        CheckConstraint(
            "revision >= 1 AND roadmap_issue_number >= 1 AND version >= 1",
            name="ck_roadmap_application_numbers",
        ),
        CheckConstraint(
            "length(base_body_hash) = 64 AND length(expected_body_hash) = 64",
            name="ck_roadmap_application_hashes",
        ),
        Index(
            "ix_roadmap_applications_target_status",
            "repository_full_name",
            "roadmap_issue_number",
            "status",
        ),
    )

    application_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("roadmap_change_proposals.proposal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_full_name: Mapped[str] = mapped_column(String(300), nullable=False)
    roadmap_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_body: Mapped[str] = mapped_column(Text, nullable=False)
    base_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_body: Mapped[str] = mapped_column(Text, nullable=False)
    expected_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_command_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_remote_body_hash: Mapped[str | None] = mapped_column(String(64))


class RoadmapChangeApplicationAttemptRecord(Base):
    __tablename__ = "roadmap_change_application_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="ck_roadmap_application_attempt_number"),
        CheckConstraint(
            "outcome IN ('PREPARED','APPLYING','NOT_EMITTED','WRITE_RETURNED','APPLIED','CONFLICT','RECONCILIATION_REQUIRED')",
            name="ck_roadmap_application_attempt_outcome",
        ),
        UniqueConstraint(
            "application_id",
            "attempt_number",
            name="uq_roadmap_application_attempt_number",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("roadmap_change_applications.application_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    command_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    patch_may_have_been_emitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)


class RoadmapTargetFenceRecord(Base):
    __tablename__ = "roadmap_target_fences"

    repository_full_name: Mapped[str] = mapped_column(String(300), primary_key=True)
    roadmap_issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    active_application_id: Mapped[str | None] = mapped_column(
        ForeignKey("roadmap_change_applications.application_id", ondelete="RESTRICT")
    )


@dataclass(frozen=True)
class RoadmapTargetFence:
    repository_full_name: str
    roadmap_issue_number: int
    generation: int
    active_application_id: UUID | None


_UUID_FIELDS = {"application_id", "proposal_id", "application_command_id", "attempt_id", "command_id"}


def _values(entity):
    result = {}
    for key, value in asdict(entity).items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, (ApplicationStatus, ApplicationAttemptOutcome)):
            result[key] = value.value
        else:
            result[key] = value
    return result


def _as_utc(value: datetime | None):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _entity(record, cls):
    if record is None:
        return None
    data = {field.name: getattr(record, field.name) for field in fields(cls)}
    for key, value in data.items():
        if key in _UUID_FIELDS and value is not None:
            data[key] = UUID(value)
        elif isinstance(value, datetime):
            data[key] = _as_utc(value)
    if cls is RoadmapChangeApplication:
        data["status"] = ApplicationStatus(data["status"])
    elif cls is RoadmapChangeApplicationAttempt:
        data["outcome"] = ApplicationAttemptOutcome(data["outcome"])
    return cls(**data)


class SqlAlchemyRoadmapChangeApplicationRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, identity):
        return _entity(
            self.session.get(RoadmapChangeApplicationRecord, str(identity)),
            RoadmapChangeApplication,
        )

    def by_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeApplicationRecord).where(
                RoadmapChangeApplicationRecord.application_command_id == str(identity)
            )
        )
        return _entity(record, RoadmapChangeApplication)

    def list_for_proposal(self, proposal_id):
        records = self.session.scalars(
            select(RoadmapChangeApplicationRecord)
            .where(RoadmapChangeApplicationRecord.proposal_id == str(proposal_id))
            .order_by(
                RoadmapChangeApplicationRecord.created_at,
                RoadmapChangeApplicationRecord.application_id,
            )
        )
        return [_entity(record, RoadmapChangeApplication) for record in records]

    def add(self, application):
        self.session.add(RoadmapChangeApplicationRecord(**_values(application)))

    def save(self, application, expected_version):
        record = self.session.get(
            RoadmapChangeApplicationRecord,
            str(application.application_id),
        )
        if record is None or record.version != expected_version:
            raise OrchestrationConflict("RoadmapChangeApplication was changed by another command")
        for key, value in _values(application).items():
            setattr(record, key, value)


class SqlAlchemyRoadmapChangeApplicationAttemptRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, identity):
        return _entity(
            self.session.get(RoadmapChangeApplicationAttemptRecord, str(identity)),
            RoadmapChangeApplicationAttempt,
        )

    def by_command(self, identity):
        record = self.session.scalar(
            select(RoadmapChangeApplicationAttemptRecord).where(
                RoadmapChangeApplicationAttemptRecord.command_id == str(identity)
            )
        )
        return _entity(record, RoadmapChangeApplicationAttempt)

    def list_for_application(self, application_id):
        records = self.session.scalars(
            select(RoadmapChangeApplicationAttemptRecord)
            .where(
                RoadmapChangeApplicationAttemptRecord.application_id == str(application_id)
            )
            .order_by(RoadmapChangeApplicationAttemptRecord.attempt_number)
        )
        return [_entity(record, RoadmapChangeApplicationAttempt) for record in records]

    def add(self, attempt):
        self.session.add(RoadmapChangeApplicationAttemptRecord(**_values(attempt)))

    def save(self, attempt):
        record = self.session.get(
            RoadmapChangeApplicationAttemptRecord,
            str(attempt.attempt_id),
        )
        if record is None:
            raise OrchestrationConflict("Roadmap application attempt not found")
        for key, value in _values(attempt).items():
            setattr(record, key, value)


class SqlAlchemyRoadmapTargetFenceRepository:
    def __init__(self, session: Session):
        self.session = session

    def snapshot(self, repository_full_name: str, roadmap_issue_number: int) -> RoadmapTargetFence:
        record = self.session.get(
            RoadmapTargetFenceRecord,
            (repository_full_name, int(roadmap_issue_number)),
        )
        if record is None:
            return RoadmapTargetFence(
                repository_full_name,
                int(roadmap_issue_number),
                0,
                None,
            )
        return RoadmapTargetFence(
            record.repository_full_name,
            record.roadmap_issue_number,
            record.generation,
            UUID(record.active_application_id) if record.active_application_id else None,
        )

    def claim(
        self,
        repository_full_name: str,
        roadmap_issue_number: int,
        application_id: UUID,
    ) -> RoadmapTargetFence:
        record = self.session.get(
            RoadmapTargetFenceRecord,
            (repository_full_name, int(roadmap_issue_number)),
        )
        if record is None:
            record = RoadmapTargetFenceRecord(
                repository_full_name=repository_full_name,
                roadmap_issue_number=int(roadmap_issue_number),
                generation=1,
                active_application_id=str(application_id),
            )
            self.session.add(record)
        else:
            if (
                record.active_application_id is not None
                and record.active_application_id != str(application_id)
            ):
                raise OrchestrationConflict(
                    "Another roadmap application is active for this target"
                )
            if record.active_application_id != str(application_id):
                record.generation += 1
            record.active_application_id = str(application_id)
        return RoadmapTargetFence(
            repository_full_name,
            int(roadmap_issue_number),
            record.generation,
            application_id,
        )

    def release(
        self,
        repository_full_name: str,
        roadmap_issue_number: int,
        application_id: UUID,
    ) -> None:
        record = self.session.get(
            RoadmapTargetFenceRecord,
            (repository_full_name, int(roadmap_issue_number)),
        )
        if record is None:
            return
        if record.active_application_id not in {None, str(application_id)}:
            raise OrchestrationConflict("Roadmap target fence belongs to another application")
        record.active_application_id = None
