from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum


class FinalizationOperation(StrEnum):
    SYNC_BRANCH = "SYNC_BRANCH"
    MERGE_PR = "MERGE_PR"


class FinalizationAttemptStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class PullRequestFinalizationAttempt:
    idempotency_key: str
    project_id: str
    work_item_id: str
    pr_number: int
    operation: FinalizationOperation
    expected_head_sha: str
    base_sha: str | None
    status: FinalizationAttemptStatus
    error_code: str | None
    message: str | None
    requires_dev: bool
    resulting_head_sha: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def claim(
        cls,
        *,
        idempotency_key: str,
        project_id: str,
        work_item_id: str,
        pr_number: int,
        operation: FinalizationOperation,
        expected_head_sha: str,
        base_sha: str | None,
        now: datetime | None = None,
    ) -> "PullRequestFinalizationAttempt":
        current = now or datetime.now(timezone.utc)
        return cls(
            idempotency_key=idempotency_key,
            project_id=project_id,
            work_item_id=work_item_id,
            pr_number=pr_number,
            operation=operation,
            expected_head_sha=expected_head_sha,
            base_sha=base_sha,
            status=FinalizationAttemptStatus.IN_PROGRESS,
            error_code=None,
            message=None,
            requires_dev=False,
            resulting_head_sha=None,
            created_at=current,
            updated_at=current,
        )

    def complete(
        self,
        *,
        status: FinalizationAttemptStatus,
        error_code: str | None = None,
        message: str | None = None,
        requires_dev: bool = False,
        resulting_head_sha: str | None = None,
        now: datetime | None = None,
    ) -> "PullRequestFinalizationAttempt":
        if status is FinalizationAttemptStatus.IN_PROGRESS:
            raise ValueError("A completed finalization attempt cannot remain IN_PROGRESS")
        return replace(
            self,
            status=status,
            error_code=error_code,
            message=message,
            requires_dev=requires_dev,
            resulting_head_sha=resulting_head_sha,
            updated_at=now or datetime.now(timezone.utc),
        )
