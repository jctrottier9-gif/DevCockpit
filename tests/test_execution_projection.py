from app.domain.execution import (
    BranchEvidence,
    CiState,
    ExecutionEvidence,
    ExecutionState,
    NextAction,
    PullRequestEvidence,
    WorkflowRunEvidence,
    derive_execution_projection,
    pull_request_matches_work_item,
)
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType


WORK_ITEM = WorkItem(
    key="DC-021",
    type=WorkItemType.WORK,
    status=WorkItemStatus.READY,
    parent="#7",
    lane="MAIN",
    title="Projection d'exécution, CI et follow-up DEV",
)


def pr(
    *,
    number: int = 22,
    title: str = "DC-021 — execution projection",
    body: str = "",
    branch: str = "dc-021-execution-ci",
    head_sha: str = "abc123",
    state: str = "open",
    merged: bool = False,
    mergeable: bool | None = None,
    merged_at: str | None = None,
    auto_merge_enabled: bool = False,
    base_sha: str | None = None,
    behind_by: int | None = None,
    mergeable_state: str | None = None,
) -> PullRequestEvidence:
    return PullRequestEvidence(
        number=number,
        title=title,
        body=body,
        branch=branch,
        head_sha=head_sha,
        state=state,
        merged=merged,
        mergeable=mergeable,
        base_branch="main" if base_sha is not None else None,
        base_sha=base_sha,
        behind_by=behind_by,
        mergeable_state=mergeable_state,
        auto_merge_enabled=auto_merge_enabled,
        url=f"https://github.example/pr/{number}",
        updated_at="2026-10-01T12:00:00Z",
        merged_at=merged_at,
    )


def run(
    *,
    run_id: int = 123,
    status: str = "completed",
    conclusion: str | None = "success",
    attempt: int = 1,
    sha: str = "abc123",
    failed_jobs: tuple[str, ...] = (),
) -> WorkflowRunEvidence:
    return WorkflowRunEvidence(
        run_id=run_id,
        name="CI",
        status=status,
        conclusion=conclusion,
        attempt=attempt,
        head_sha=sha,
        url=f"https://github.example/actions/{run_id}",
        failed_jobs=failed_jobs,
    )


def test_ready_without_strong_github_evidence_starts_dev() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(default_branch="main"),
    )

    assert projection.state is ExecutionState.READY
    assert projection.next_action is NextAction.START_DEV


def test_developing_requires_strong_branch_that_is_ahead_of_main() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            branches=(BranchEvidence("dc-021-execution-ci", "abc123", 2),),
        ),
    )

    assert projection.state is ExecutionState.DEVELOPING
    assert projection.next_action is NextAction.WAIT_FOR_PR


def test_open_pr_without_ci_is_pr_open() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(default_branch="main", pull_requests=(pr(),)),
    )

    assert projection.state is ExecutionState.PR_OPEN
    assert projection.ci is not None
    assert projection.ci.state is CiState.NOT_OBSERVED


def test_current_head_running_ci_waits_without_follow_up() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(),),
            workflow_runs=(run(status="in_progress", conclusion=None),),
        ),
    )

    assert projection.state is ExecutionState.CI_RUNNING
    assert projection.next_action is NextAction.WAIT


def test_current_head_red_ci_routes_to_fix_ci() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(),),
            workflow_runs=(
                run(conclusion="failure", failed_jobs=("backend / pytest",)),
            ),
        ),
    )

    assert projection.state is ExecutionState.CI_RED
    assert projection.next_action is NextAction.FIX_CI
    assert projection.ci is not None
    assert projection.ci.failed_jobs == ("backend / pytest",)
    assert projection.ci.failure_run_id == 123


def test_old_sha_failure_is_not_mixed_with_current_head() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(head_sha="newsha"),),
            workflow_runs=(
                run(sha="oldsha", conclusion="failure"),
                run(run_id=124, sha="newsha", status="in_progress", conclusion=None),
            ),
        ),
    )

    assert projection.state is ExecutionState.CI_RUNNING


def test_green_pr_behind_base_requires_branch_sync_before_merge() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(
                pr(
                    mergeable=True,
                    auto_merge_enabled=True,
                    base_sha="base-1",
                    behind_by=1,
                ),
            ),
            workflow_runs=(run(conclusion="success"),),
        ),
    )

    assert projection.state is ExecutionState.BASE_OUTDATED
    assert projection.next_action is NextAction.SYNC_BRANCH
    assert projection.pull_request.base_sha == "base-1"
    assert projection.pull_request.behind_by == 1


def test_green_mergeable_pr_with_github_behind_state_requires_branch_sync_even_when_compare_is_zero() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(
                pr(
                    mergeable=True,
                    auto_merge_enabled=True,
                    base_sha="current-main",
                    behind_by=0,
                    mergeable_state="behind",
                ),
            ),
            workflow_runs=(run(conclusion="success"),),
        ),
    )

    assert projection.state is ExecutionState.BASE_OUTDATED
    assert projection.next_action is NextAction.SYNC_BRANCH
    assert projection.pull_request is not None
    assert projection.pull_request.mergeable_state == "behind"


def test_green_mergeable_pr_is_ready_to_merge() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(mergeable=True),),
            workflow_runs=(run(conclusion="success"),),
        ),
    )

    assert projection.state is ExecutionState.READY_TO_MERGE
    assert projection.next_action is NextAction.MERGE_PR


def test_green_mergeable_pr_with_auto_merge_armed_waits_without_follow_up() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(mergeable=True, auto_merge_enabled=True),),
            workflow_runs=(run(conclusion="success"),),
        ),
    )

    assert projection.state is ExecutionState.READY_TO_MERGE
    assert projection.next_action is NextAction.WAIT_AUTO_MERGE


def test_merged_green_pr_while_roadmap_ready_requires_reconciliation() -> None:
    merged = pr(
        state="closed",
        merged=True,
        mergeable=None,
        merged_at="2026-10-01T13:00:00Z",
    )
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(merged,),
            workflow_runs=(run(conclusion="success"),),
        ),
    )

    assert projection.state is ExecutionState.ROADMAP_UPDATE_REQUIRED
    assert projection.next_action is NextAction.RECONCILE_ROADMAP


def test_two_open_strong_prs_fail_closed() -> None:
    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(
            default_branch="main",
            pull_requests=(pr(number=30), pr(number=31)),
        ),
    )

    assert projection.state is ExecutionState.BLOCKED
    assert {item.code for item in projection.diagnostics} == {"AMBIGUOUS_DELIVERY"}


def test_incidental_body_mention_does_not_associate_another_pr() -> None:
    unrelated = pr(
        title="DC-020 — GitHub projects and canonical roadmap parser",
        branch="dc-020-roadmap-projects",
        body="No DC-021 work is included. DC-021 is intentionally out of scope.",
    )

    assert pull_request_matches_work_item(unrelated, WORK_ITEM.key) is False

    projection = derive_execution_projection(
        WORK_ITEM,
        ExecutionEvidence(default_branch="main", pull_requests=(unrelated,)),
    )
    assert projection.state is ExecutionState.READY


def test_explicit_structured_reference_is_strong_identity() -> None:
    structured = pr(
        title="Fix execution projection",
        branch="fix/execution",
        body="Summary\n\nWork-Item: DC-021\n",
    )

    assert pull_request_matches_work_item(structured, WORK_ITEM.key) is True
