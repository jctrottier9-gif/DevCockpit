from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Protocol

from app.domain.execution import (
    ExecutionDiagnostic,
    ExecutionProjection,
    ExecutionState,
    NextAction,
)
from app.domain.pr_finalization import (
    FinalizationAttemptStatus,
    FinalizationOperation,
    PullRequestFinalizationAttempt,
)
from app.domain.project import Project


@dataclass(frozen=True, slots=True)
class FinalizationMutationResult:
    status: FinalizationAttemptStatus
    error_code: str | None = None
    message: str | None = None
    requires_dev: bool = False
    resulting_head_sha: str | None = None


class PullRequestFinalizer(Protocol):
    def sync_branch(
        self,
        project: Project,
        *,
        pr_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> FinalizationMutationResult: ...

    def merge_pull_request(
        self,
        project: Project,
        *,
        pr_number: int,
        expected_head_sha: str,
    ) -> FinalizationMutationResult: ...


def finalization_idempotency_key(
    project: Project,
    projection: ExecutionProjection,
) -> str:
    work_item = projection.work_item
    pull_request = projection.pull_request
    if work_item is None or pull_request is None:
        raise ValueError("PR finalization requires WorkItem and pull-request evidence")

    if projection.next_action is NextAction.SYNC_BRANCH:
        if not pull_request.base_sha:
            raise ValueError("Branch synchronization requires an observed base SHA")
        raw = (
            f"finalization:{project.project_id}:{work_item.key}:"
            f"SYNC_BRANCH:pr{pull_request.number}:"
            f"{pull_request.head_sha}:{pull_request.base_sha}:v1"
        )
    elif projection.next_action is NextAction.MERGE_PR:
        raw = (
            f"finalization:{project.project_id}:{work_item.key}:"
            f"MERGE_PR:pr{pull_request.number}:{pull_request.head_sha}:v1"
        )
    else:
        raise ValueError("Projection does not require a deterministic PR mutation")

    if len(raw) <= 200:
        return raw
    return f"finalization:{sha256(raw.encode('utf-8')).hexdigest()}:v1"


def operation_for(projection: ExecutionProjection) -> FinalizationOperation:
    if projection.next_action is NextAction.SYNC_BRANCH:
        return FinalizationOperation.SYNC_BRANCH
    if projection.next_action is NextAction.MERGE_PR:
        return FinalizationOperation.MERGE_PR
    raise ValueError("Projection does not require deterministic finalization")


def overlay_finalization_attempt(
    project: Project,
    projection: ExecutionProjection,
    *,
    attempts,
) -> ExecutionProjection:
    if projection.next_action not in {NextAction.SYNC_BRANCH, NextAction.MERGE_PR}:
        return projection

    try:
        key = finalization_idempotency_key(project, projection)
    except ValueError:
        return projection
    attempt = attempts.get_by_idempotency_key(key)
    if attempt is None:
        return projection

    diagnostic = ExecutionDiagnostic(
        code=(
            attempt.error_code
            or (
                "FINALIZATION_IN_PROGRESS"
                if attempt.status is FinalizationAttemptStatus.IN_PROGRESS
                else "FINALIZATION_ACCEPTED"
            )
        ),
        message=attempt.message or (
            "GitHub mutation outcome is still being reconciled."
            if attempt.status is FinalizationAttemptStatus.IN_PROGRESS
            else "GitHub accepted the deterministic finalization mutation."
        ),
    )

    if attempt.status is FinalizationAttemptStatus.BLOCKED:
        if attempt.operation is FinalizationOperation.SYNC_BRANCH:
            return replace(
                projection,
                state=ExecutionState.BRANCH_SYNC_BLOCKED,
                next_action=(
                    NextAction.RESOLVE_BRANCH_SYNC
                    if attempt.requires_dev
                    else NextAction.RESOLVE_BLOCKER
                ),
                diagnostics=(*projection.diagnostics, diagnostic),
            )
        return replace(
            projection,
            state=ExecutionState.MERGE_BLOCKED,
            next_action=NextAction.RESOLVE_MERGE_BLOCKER,
            diagnostics=(*projection.diagnostics, diagnostic),
        )

    return replace(
        projection,
        next_action=NextAction.WAIT,
        diagnostics=(*projection.diagnostics, diagnostic),
    )


def branch_sync_follow_up_key(
    project: Project,
    projection: ExecutionProjection,
    attempt: PullRequestFinalizationAttempt,
) -> str:
    work_item = projection.work_item
    if (
        work_item is None
        or projection.state is not ExecutionState.BRANCH_SYNC_BLOCKED
        or not attempt.requires_dev
    ):
        raise ValueError("Branch-sync follow-up requires a DEV-resolvable sync blocker")
    raw = (
        f"execution:{project.project_id}:{work_item.key}:DEV:BRANCH_SYNC_BLOCKED:"
        f"{attempt.idempotency_key}:v1"
    )
    if len(raw) <= 200:
        return raw
    return f"execution:{sha256(raw.encode('utf-8')).hexdigest()}:v1"


def build_branch_sync_blocked_follow_up(
    project: Project,
    projection: ExecutionProjection,
    attempt: PullRequestFinalizationAttempt,
) -> str:
    work_item = projection.work_item
    pull_request = projection.pull_request
    if (
        work_item is None
        or pull_request is None
        or projection.state is not ExecutionState.BRANCH_SYNC_BLOCKED
        or not attempt.requires_dev
    ):
        raise ValueError("Branch-sync follow-up requires blocked pull-request evidence")

    return f"""La synchronisation mécanique de la branche de la PR #{pull_request.number} pour {work_item.key} avec sa base a été refusée parce qu'une intervention DEV est nécessaire.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR : #{pull_request.number}
Head SHA observé : {pull_request.head_sha}
Base SHA observé : {pull_request.base_sha or 'non disponible'}
Blocage GitHub : {attempt.message or attempt.error_code or 'BRANCH_SYNC_BLOCKED'}

Reprends la même session DEV uniquement pour résoudre le conflit de synchronisation de branche.

- resynchronise-toi avec le vrai main et relis AGENTS.md;
- inspecte la PR et les changements concurrents avant toute modification;
- résous uniquement les conflits nécessaires pour remettre la branche à jour;
- ne change pas le scope fonctionnel de {work_item.key};
- pousse le nouveau head et laisse la CI repartir sur ce nouveau SHA;
- l'ancienne CI verte ne vaut plus après le changement de head;
- rapporte le nouveau head SHA puis ARRÊTE ton tour DEV;
- ne fusionne pas manuellement la PR et ne commence pas la tranche suivante.

DevCockpit reprendra ensuite le cycle normal CI / finalisation GitHub.
"""
