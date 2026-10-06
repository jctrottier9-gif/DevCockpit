from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from app.application.github_wait_watchdogs import (
    GitHubWaitWatchdogKind,
    project_execution_github_wait_watchdog,
    watchdog_dispatch_key,
)
from app.domain.execution import (
    CiState,
    CiSummary,
    ExecutionProjection,
    ExecutionState,
    NextAction,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.project import Project


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def pull_request(
    *,
    head_sha: str = "head-1",
    created_at: str = "2026-10-06T13:00:00Z",
    updated_at: str = "2026-10-06T13:00:00Z",
    auto_merge_enabled: bool = False,
) -> PullRequestEvidence:
    return PullRequestEvidence(
        number=108,
        title="DC-072 — watchdogs",
        body="",
        branch="dc-072-github-wait-watchdogs",
        head_sha=head_sha,
        state="open",
        merged=False,
        mergeable=True,
        base_branch="main",
        base_sha="base-1",
        behind_by=0,
        auto_merge_enabled=auto_merge_enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


def execution(
    *,
    state: ExecutionState,
    next_action: NextAction,
    pr: PullRequestEvidence,
    ci_state: CiState,
    runs: tuple[WorkflowRunEvidence, ...] = (),
) -> ExecutionProjection:
    return ExecutionProjection(
        work_item=None,
        state=state,
        next_action=next_action,
        pull_request=pr,
        ci=CiSummary(
            state=ci_state,
            observed_runs=len(runs),
            failed_jobs=(),
            runs=runs,
        ),
    )


def project(projection: ExecutionProjection):
    return project_execution_github_wait_watchdog(
        projection,
        now=NOW,
        pr_no_ci_after_seconds=900,
        ci_stall_after_seconds=1800,
        auto_merge_grace_seconds=600,
    )


def test_pr_without_ci_becomes_due_and_new_pr_activity_resets_deadline():
    stale = execution(
        state=ExecutionState.PR_OPEN,
        next_action=NextAction.WAIT,
        pr=pull_request(),
        ci_state=CiState.NOT_OBSERVED,
    )
    watchdog = project(stale)

    assert watchdog is not None
    assert watchdog.kind is GitHubWaitWatchdogKind.PR_NO_CI
    assert watchdog.due is True
    assert watchdog.deadline_at.isoformat() == "2026-10-06T13:15:00+00:00"

    refreshed = replace(
        stale,
        pull_request=replace(
            stale.pull_request,
            updated_at="2026-10-06T13:55:00Z",
        ),
    )
    refreshed_watchdog = project(refreshed)

    assert refreshed_watchdog is not None
    assert refreshed_watchdog.due is False
    assert refreshed_watchdog.deadline_at.isoformat() == "2026-10-06T14:10:00+00:00"
    assert refreshed_watchdog.evidence_identity != watchdog.evidence_identity


def test_ci_stall_identity_changes_on_attempt_or_workflow_update():
    run = WorkflowRunEvidence(
        run_id=700,
        name="CI",
        status="in_progress",
        conclusion=None,
        attempt=1,
        head_sha="head-1",
        created_at="2026-10-06T12:00:00Z",
        updated_at="2026-10-06T13:00:00Z",
    )
    stalled = execution(
        state=ExecutionState.CI_RUNNING,
        next_action=NextAction.WAIT,
        pr=pull_request(),
        ci_state=CiState.RUNNING,
        runs=(run,),
    )
    watchdog = project(stalled)

    assert watchdog is not None
    assert watchdog.kind is GitHubWaitWatchdogKind.CI_STALLED
    assert watchdog.due is True

    advanced_run = replace(
        run,
        attempt=2,
        updated_at="2026-10-06T13:50:00Z",
    )
    advanced = replace(
        stalled,
        ci=replace(stalled.ci, runs=(advanced_run,)),
    )
    advanced_watchdog = project(advanced)

    assert advanced_watchdog is not None
    assert advanced_watchdog.due is False
    assert advanced_watchdog.evidence_identity != watchdog.evidence_identity


def test_auto_merge_grace_uses_current_head_and_workflow_activity():
    run = WorkflowRunEvidence(
        run_id=701,
        name="CI",
        status="completed",
        conclusion="success",
        attempt=1,
        head_sha="head-1",
        created_at="2026-10-06T12:00:00Z",
        updated_at="2026-10-06T13:30:00Z",
    )
    waiting = execution(
        state=ExecutionState.READY_TO_MERGE,
        next_action=NextAction.WAIT_AUTO_MERGE,
        pr=pull_request(auto_merge_enabled=True),
        ci_state=CiState.GREEN,
        runs=(run,),
    )
    watchdog = project(waiting)

    assert watchdog is not None
    assert watchdog.kind is GitHubWaitWatchdogKind.AUTO_MERGE_GRACE
    assert watchdog.due is True
    assert watchdog.deadline_at.isoformat() == "2026-10-06T13:40:00+00:00"

    moved = replace(
        waiting,
        pull_request=replace(
            waiting.pull_request,
            head_sha="head-2",
            updated_at="2026-10-06T13:58:00Z",
        ),
        ci=replace(
            waiting.ci,
            runs=(replace(run, head_sha="head-2", updated_at="2026-10-06T13:58:00Z"),),
        ),
    )
    moved_watchdog = project(moved)

    assert moved_watchdog is not None
    assert moved_watchdog.due is False
    assert moved_watchdog.evidence_identity != watchdog.evidence_identity


def test_watchdog_dispatch_key_is_stable_for_same_evidence_and_changes_for_new_proof():
    projection = execution(
        state=ExecutionState.PR_OPEN,
        next_action=NextAction.WAIT,
        pr=pull_request(),
        ci_state=CiState.NOT_OBSERVED,
    )
    watchdog = project(projection)
    assert watchdog is not None

    first = watchdog_dispatch_key(PROJECT, "DC-072", watchdog)
    second = watchdog_dispatch_key(PROJECT, "DC-072", watchdog)
    assert first == second

    refreshed = project(
        replace(
            projection,
            pull_request=replace(
                projection.pull_request,
                updated_at="2026-10-06T13:55:00Z",
            ),
        )
    )
    assert refreshed is not None
    assert watchdog_dispatch_key(PROJECT, "DC-072", refreshed) != first
