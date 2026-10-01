from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from app.domain.roadmap import WorkItem


_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_RUNNING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


class ExecutionState(StrEnum):
    READY = "READY"
    DEVELOPING = "DEVELOPING"
    PR_OPEN = "PR_OPEN"
    CI_RUNNING = "CI_RUNNING"
    CI_RED = "CI_RED"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    ROADMAP_UPDATE_REQUIRED = "ROADMAP_UPDATE_REQUIRED"
    BLOCKED = "BLOCKED"


class NextAction(StrEnum):
    START_DEV = "START_DEV"
    WAIT_FOR_PR = "WAIT_FOR_PR"
    WAIT = "WAIT"
    FIX_CI = "FIX_CI"
    MERGE_PR = "MERGE_PR"
    RECONCILE_ROADMAP = "RECONCILE_ROADMAP"
    RESOLVE_BLOCKER = "RESOLVE_BLOCKER"
    NONE = "NONE"


class CiState(StrEnum):
    NOT_OBSERVED = "NOT_OBSERVED"
    RUNNING = "RUNNING"
    RED = "RED"
    GREEN = "GREEN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExecutionDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BranchEvidence:
    name: str
    sha: str
    ahead_by: int


@dataclass(frozen=True, slots=True)
class PullRequestEvidence:
    number: int
    title: str
    body: str
    branch: str
    head_sha: str
    state: str
    merged: bool
    mergeable: bool | None
    url: str | None = None
    updated_at: str | None = None
    merged_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRunEvidence:
    run_id: int
    name: str
    status: str
    conclusion: str | None
    attempt: int
    head_sha: str
    url: str | None = None
    failed_jobs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    default_branch: str
    branches: tuple[BranchEvidence, ...] = ()
    pull_requests: tuple[PullRequestEvidence, ...] = ()
    workflow_runs: tuple[WorkflowRunEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class CiSummary:
    state: CiState
    observed_runs: int
    failed_jobs: tuple[str, ...]
    runs: tuple[WorkflowRunEvidence, ...]
    failure_run_id: int | None = None
    failure_run_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionProjection:
    work_item: WorkItem | None
    state: ExecutionState
    next_action: NextAction
    branch: BranchEvidence | None = None
    pull_request: PullRequestEvidence | None = None
    ci: CiSummary | None = None
    diagnostics: tuple[ExecutionDiagnostic, ...] = ()


def title_matches_work_item(title: str, work_item_key: str) -> bool:
    pattern = re.compile(
        rf"^\s*{re.escape(work_item_key)}(?=$|[\s:—–-])",
        re.IGNORECASE,
    )
    return bool(pattern.search(title))


def branch_matches_work_item(branch: str, work_item_key: str) -> bool:
    key = work_item_key.lower()
    for segment in branch.lower().split("/"):
        if segment == key or segment.startswith(key + "-") or segment.startswith(key + "_"):
            return True
    return False


def structured_reference_matches_work_item(body: str, work_item_key: str) -> bool:
    pattern = re.compile(
        rf"^\s*Work-Item:\s*{re.escape(work_item_key)}\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    return bool(pattern.search(body))


def pull_request_matches_work_item(
    pull_request: PullRequestEvidence,
    work_item_key: str,
) -> bool:
    return (
        title_matches_work_item(pull_request.title, work_item_key)
        or branch_matches_work_item(pull_request.branch, work_item_key)
        or structured_reference_matches_work_item(pull_request.body, work_item_key)
    )


def summarize_ci(
    runs: tuple[WorkflowRunEvidence, ...],
    *,
    head_sha: str,
) -> CiSummary:
    current = tuple(run for run in runs if run.head_sha == head_sha)
    if not current:
        return CiSummary(CiState.NOT_OBSERVED, 0, (), ())

    failed = tuple(
        run
        for run in current
        if run.status == "completed" and run.conclusion in _FAILURE_CONCLUSIONS
    )
    failed_jobs = tuple(sorted({job for run in failed for job in run.failed_jobs}))
    if failed:
        primary = max(failed, key=lambda run: (run.run_id, run.attempt))
        return CiSummary(
            state=CiState.RED,
            observed_runs=len(current),
            failed_jobs=failed_jobs,
            runs=current,
            failure_run_id=primary.run_id,
            failure_run_attempt=primary.attempt,
        )

    if any(run.status in _RUNNING_STATUSES for run in current):
        return CiSummary(CiState.RUNNING, len(current), (), current)

    if all(
        run.status == "completed" and run.conclusion in _GREEN_CONCLUSIONS
        for run in current
    ):
        return CiSummary(CiState.GREEN, len(current), (), current)

    return CiSummary(CiState.UNKNOWN, len(current), (), current)


def blocked_projection(
    *,
    work_item: WorkItem | None,
    code: str,
    message: str,
) -> ExecutionProjection:
    return ExecutionProjection(
        work_item=work_item,
        state=ExecutionState.BLOCKED,
        next_action=NextAction.RESOLVE_BLOCKER,
        diagnostics=(ExecutionDiagnostic(code=code, message=message),),
    )


def derive_execution_projection(
    work_item: WorkItem,
    evidence: ExecutionEvidence,
) -> ExecutionProjection:
    matching_prs = tuple(
        pr
        for pr in evidence.pull_requests
        if pull_request_matches_work_item(pr, work_item.key)
    )
    open_prs = tuple(pr for pr in matching_prs if pr.state == "open" and not pr.merged)

    if len(open_prs) > 1:
        return blocked_projection(
            work_item=work_item,
            code="AMBIGUOUS_DELIVERY",
            message=f"Multiple open pull requests are strongly associated with {work_item.key}.",
        )

    if len(open_prs) == 1:
        pull_request = open_prs[0]
        ci = summarize_ci(evidence.workflow_runs, head_sha=pull_request.head_sha)

        if ci.state is CiState.RED:
            return ExecutionProjection(
                work_item=work_item,
                state=ExecutionState.CI_RED,
                next_action=NextAction.FIX_CI,
                branch=_branch_for(evidence.branches, pull_request.branch),
                pull_request=pull_request,
                ci=ci,
            )

        if ci.state is CiState.RUNNING:
            return ExecutionProjection(
                work_item=work_item,
                state=ExecutionState.CI_RUNNING,
                next_action=NextAction.WAIT,
                branch=_branch_for(evidence.branches, pull_request.branch),
                pull_request=pull_request,
                ci=ci,
            )

        if ci.state is CiState.GREEN and pull_request.mergeable is True:
            return ExecutionProjection(
                work_item=work_item,
                state=ExecutionState.READY_TO_MERGE,
                next_action=NextAction.MERGE_PR,
                branch=_branch_for(evidence.branches, pull_request.branch),
                pull_request=pull_request,
                ci=ci,
            )

        diagnostics: tuple[ExecutionDiagnostic, ...] = ()
        if ci.state is CiState.GREEN and pull_request.mergeable is not True:
            diagnostics = (
                ExecutionDiagnostic(
                    code="PR_NOT_MERGEABLE",
                    message="Current CI is green but GitHub does not report the pull request as mergeable.",
                ),
            )
        return ExecutionProjection(
            work_item=work_item,
            state=ExecutionState.PR_OPEN,
            next_action=NextAction.WAIT,
            branch=_branch_for(evidence.branches, pull_request.branch),
            pull_request=pull_request,
            ci=ci,
            diagnostics=diagnostics,
        )

    merged_prs = tuple(pr for pr in matching_prs if pr.merged)
    if merged_prs:
        pull_request = max(
            merged_prs,
            key=lambda pr: (pr.merged_at or "", pr.updated_at or "", pr.number),
        )
        ci = summarize_ci(evidence.workflow_runs, head_sha=pull_request.head_sha)
        if ci.state is CiState.GREEN:
            return ExecutionProjection(
                work_item=work_item,
                state=ExecutionState.ROADMAP_UPDATE_REQUIRED,
                next_action=NextAction.RECONCILE_ROADMAP,
                branch=_branch_for(evidence.branches, pull_request.branch),
                pull_request=pull_request,
                ci=ci,
                diagnostics=(
                    ExecutionDiagnostic(
                        code="ROADMAP_STALE_AFTER_MERGE",
                        message=(
                            f"{work_item.key} is still READY in the canonical roadmap "
                            "although its strongly associated pull request is merged with green CI."
                        ),
                    ),
                ),
            )
        return ExecutionProjection(
            work_item=work_item,
            state=ExecutionState.MERGED,
            next_action=NextAction.WAIT,
            branch=_branch_for(evidence.branches, pull_request.branch),
            pull_request=pull_request,
            ci=ci,
        )

    active_branches = tuple(
        branch
        for branch in evidence.branches
        if branch.ahead_by > 0 and branch_matches_work_item(branch.name, work_item.key)
    )
    if len(active_branches) > 1:
        return blocked_projection(
            work_item=work_item,
            code="AMBIGUOUS_BRANCH",
            message=f"Multiple active branches are strongly associated with {work_item.key}.",
        )

    if len(active_branches) == 1:
        return ExecutionProjection(
            work_item=work_item,
            state=ExecutionState.DEVELOPING,
            next_action=NextAction.WAIT_FOR_PR,
            branch=active_branches[0],
        )

    return ExecutionProjection(
        work_item=work_item,
        state=ExecutionState.READY,
        next_action=NextAction.START_DEV,
    )


def _branch_for(
    branches: tuple[BranchEvidence, ...],
    branch_name: str,
) -> BranchEvidence | None:
    return next((branch for branch in branches if branch.name == branch_name), None)
