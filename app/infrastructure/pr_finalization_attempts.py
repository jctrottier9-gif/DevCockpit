from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.pr_finalization import (
    FinalizationAttemptStatus,
    FinalizationOperation,
    PullRequestFinalizationAttempt,
)
from app.infrastructure.database import Base


class PullRequestFinalizationAttemptRecord(Base):
    __tablename__ = "pull_request_finalization_attempts"

    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(200), nullable=False)
    work_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_dev: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resulting_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyPullRequestFinalizationAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> PullRequestFinalizationAttempt | None:
        record = self._session.get(PullRequestFinalizationAttemptRecord, idempotency_key)
        return _domain(record) if record is not None else None

    def add(self, attempt: PullRequestFinalizationAttempt) -> None:
        self._session.add(_record(attempt))

    def save(self, attempt: PullRequestFinalizationAttempt) -> None:
        record = self._session.get(
            PullRequestFinalizationAttemptRecord,
            attempt.idempotency_key,
        )
        if record is None:
            self.add(attempt)
            return
        record.status = attempt.status.value
        record.error_code = attempt.error_code
        record.message = attempt.message
        record.requires_dev = attempt.requires_dev
        record.resulting_head_sha = attempt.resulting_head_sha
        record.updated_at = attempt.updated_at


def _record(attempt: PullRequestFinalizationAttempt) -> PullRequestFinalizationAttemptRecord:
    return PullRequestFinalizationAttemptRecord(
        idempotency_key=attempt.idempotency_key,
        project_id=attempt.project_id,
        work_item_id=attempt.work_item_id,
        pr_number=attempt.pr_number,
        operation=attempt.operation.value,
        expected_head_sha=attempt.expected_head_sha,
        base_sha=attempt.base_sha,
        status=attempt.status.value,
        error_code=attempt.error_code,
        message=attempt.message,
        requires_dev=attempt.requires_dev,
        resulting_head_sha=attempt.resulting_head_sha,
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain(record: PullRequestFinalizationAttemptRecord) -> PullRequestFinalizationAttempt:
    return PullRequestFinalizationAttempt(
        idempotency_key=record.idempotency_key,
        project_id=record.project_id,
        work_item_id=record.work_item_id,
        pr_number=record.pr_number,
        operation=FinalizationOperation(record.operation),
        expected_head_sha=record.expected_head_sha,
        base_sha=record.base_sha,
        status=FinalizationAttemptStatus(record.status),
        error_code=record.error_code,
        message=record.message,
        requires_dev=record.requires_dev,
        resulting_head_sha=record.resulting_head_sha,
        created_at=_utc(record.created_at),
        updated_at=_utc(record.updated_at),
    )
