from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from app.application.parallel_executions import evaluate_project_parallel_dev_executions
from app.application.pr_finalization import FinalizationMutationResult
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import (
    ExecutionEvidence,
    ExecutionState,
    NextAction,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.pr_finalization import FinalizationAttemptStatus
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatchStatus
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | READY | #1 | MAIN | Deterministic finalization | - | -
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            ROADMAP,
            "2026-10-05T20:00:00Z",
        )


class MutableEvidenceReader:
    def __init__(self, evidence: ExecutionEvidence) -> None:
        self.evidence = evidence

    def read(self, project, work_item) -> ExecutionEvidence:
        return self.evidence


class FakeFinalizer:
    def __init__(
        self,
        *,
        sync_result: FinalizationMutationResult | None = None,
        merge_result: FinalizationMutationResult | None = None,
        on_sync=None,
        on_merge=None,
    ) -> None:
        self.sync_result = sync_result
        self.merge_result = merge_result
        self.on_sync = on_sync
        self.on_merge = on_merge
        self.sync_calls: list[tuple[int, str, str]] = []
        self.merge_calls: list[tuple[int, str, str | None]] = []

    def sync_branch(
        self,
        project,
        *,
        pr_number,
        expected_head_sha,
        expected_base_sha,
    ):
        self.sync_calls.append((pr_number, expected_head_sha, expected_base_sha))
        if self.on_sync is not None:
            self.on_sync()
        return self.sync_result or FinalizationMutationResult(
            FinalizationAttemptStatus.SUCCEEDED,
            message="accepted",
        )

    def merge_pull_request(
        self,
        project,
        *,
        pr_number,
        expected_head_sha,
        expected_base_sha,
    ):
        self.merge_calls.append((pr_number, expected_head_sha, expected_base_sha))
        if self.on_merge is not None:
            self.on_merge()
        return self.merge_result or FinalizationMutationResult(
            FinalizationAttemptStatus.SUCCEEDED,
            message="merged",
            resulting_head_sha="merge-sha",
        )


def evidence(
    *,
    head_sha: str = "head-1",
    base_sha: str = "base-1",
    behind_by: int = 0,
    auto_merge_enabled: bool = False,
    mergeable: bool | None = True,
    merged: bool = False,
    with_green_ci: bool = True,
) -> ExecutionEvidence:
    pr = PullRequestEvidence(
        number=71,
        title="A — deterministic finalization",
        body="",
        branch="a-finalization",
        head_sha=head_sha,
        state="closed" if merged else "open",
        merged=merged,
        mergeable=mergeable,
        base_branch="main",
        base_sha=base_sha,
        behind_by=behind_by,
        mergeable_state="clean" if mergeable else "blocked",
        auto_merge_enabled=auto_merge_enabled,
        url="https://github.example/pr/71",
        merged_at="2026-10-05T21:00:00Z" if merged else None,
    )
    runs = (
        (
            WorkflowRunEvidence(
                run_id=710,
                name="CI",
                status="completed",
                conclusion="success",
                attempt=1,
                head_sha=head_sha,
            ),
        )
        if with_green_ci
        else ()
    )
    return ExecutionEvidence(
        default_branch="main",
        pull_requests=(pr,),
        workflow_runs=runs,
    )


def uow_factory(tmp_path):
    settings = Settings(
        execution_poll_seconds=0,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'dc071.db'}",
    )
    upgrade_database(settings)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    return engine, lambda: SqlAlchemyUnitOfWork(session_factory)


def evaluate(reader, finalizer, factory):
    return evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=reader,
        uow_factory=factory,
        max_parallel_dev_executions=1,
        finalizer=finalizer,
    )


def test_behind_green_pr_syncs_once_and_old_green_ci_is_invalidated(tmp_path):
    reader = MutableEvidenceReader(evidence(behind_by=1))

    def advance_head():
        reader.evidence = evidence(
            head_sha="head-2",
            behind_by=0,
            mergeable=None,
            with_green_ci=False,
        )

    finalizer = FakeFinalizer(on_sync=advance_head)
    engine, factory = uow_factory(tmp_path)
    try:
        result = evaluate(reader, finalizer, factory)

        item = result.projection.items[0]
        assert finalizer.sync_calls == [(71, "head-1", "base-1")]
        assert finalizer.merge_calls == []
        assert item.execution.state is ExecutionState.PR_OPEN
        assert item.execution.pull_request.head_sha == "head-2"
        assert item.execution.ci.observed_runs == 0
        assert result.dispatches == ()

        second = evaluate(reader, finalizer, factory)
        assert second.projection.items[0].execution.state is ExecutionState.PR_OPEN
        assert finalizer.sync_calls == [(71, "head-1", "base-1")]
        assert finalizer.merge_calls == []
    finally:
        engine.dispose()


def test_branch_sync_conflict_is_persisted_and_prompts_dev_once(tmp_path):
    reader = MutableEvidenceReader(evidence(behind_by=1))
    finalizer = FakeFinalizer(
        sync_result=FinalizationMutationResult(
            FinalizationAttemptStatus.BLOCKED,
            error_code="BRANCH_SYNC_BLOCKED",
            message="merge conflict prevents update branch",
            requires_dev=True,
        )
    )
    engine, factory = uow_factory(tmp_path)
    try:
        first = evaluate(reader, finalizer, factory)
        item = first.projection.items[0]
        assert item.execution.state is ExecutionState.BRANCH_SYNC_BLOCKED
        assert item.execution.next_action is NextAction.RESOLVE_BRANCH_SYNC
        assert len(first.dispatches) == 1
        assert "résoudre le conflit de synchronisation" in first.dispatches[0].prompt_text
        assert len(finalizer.sync_calls) == 1

        second = evaluate(reader, finalizer, factory)
        assert second.projection.items[0].execution.state is ExecutionState.BRANCH_SYNC_BLOCKED
        assert second.dispatches == ()
        assert len(finalizer.sync_calls) == 1
    finally:
        engine.dispose()


def test_auto_merge_armed_waits_for_github_without_second_merge(tmp_path):
    reader = MutableEvidenceReader(evidence(auto_merge_enabled=True))
    finalizer = FakeFinalizer()
    engine, factory = uow_factory(tmp_path)
    try:
        result = evaluate(reader, finalizer, factory)
        item = result.projection.items[0]
        assert item.execution.state is ExecutionState.READY_TO_MERGE
        assert item.execution.next_action is NextAction.WAIT_AUTO_MERGE
        assert finalizer.merge_calls == []
        assert result.dispatches == ()
    finally:
        engine.dispose()


def test_green_up_to_date_pr_merges_in_engine_without_merge_prompt(tmp_path):
    reader = MutableEvidenceReader(evidence())

    def mark_merged():
        reader.evidence = evidence(merged=True)

    finalizer = FakeFinalizer(on_merge=mark_merged)
    engine, factory = uow_factory(tmp_path)
    try:
        result = evaluate(reader, finalizer, factory)
        item = result.projection.items[0]
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]
        assert item.execution.state is ExecutionState.ROADMAP_UPDATE_REQUIRED
        assert len(result.dispatches) == 1
        assert "réconciliation post-merge" in result.dispatches[0].prompt_text
        assert "finaliser le merge" not in result.dispatches[0].prompt_text

        evaluate(reader, finalizer, factory)
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]
    finally:
        engine.dispose()


def test_merge_refusal_is_persisted_and_not_retried(tmp_path):
    reader = MutableEvidenceReader(evidence())
    finalizer = FakeFinalizer(
        merge_result=FinalizationMutationResult(
            FinalizationAttemptStatus.BLOCKED,
            error_code="MERGE_BLOCKED",
            message="Required approving review is missing",
        )
    )
    engine, factory = uow_factory(tmp_path)
    try:
        first = evaluate(reader, finalizer, factory)
        item = first.projection.items[0]
        assert item.execution.state is ExecutionState.MERGE_BLOCKED
        assert item.execution.next_action is NextAction.RESOLVE_MERGE_BLOCKER
        assert first.dispatches == ()
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]

        second = evaluate(reader, finalizer, factory)
        assert second.projection.items[0].execution.state is ExecutionState.MERGE_BLOCKED
        assert second.dispatches == ()
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]
    finally:
        engine.dispose()


def timed_evidence(
    *,
    ci_status: str | None = None,
    ci_conclusion: str | None = None,
    auto_merge_enabled: bool = False,
    behind_by: int = 0,
    activity_at: str = "2026-10-06T12:00:00Z",
) -> ExecutionEvidence:
    base = evidence(
        auto_merge_enabled=auto_merge_enabled,
        behind_by=behind_by,
        with_green_ci=ci_status is None,
    )
    pr = replace(
        base.pull_requests[0],
        created_at=activity_at,
        updated_at=activity_at,
    )
    if ci_status is None:
        run = replace(
            base.workflow_runs[0],
            created_at=activity_at,
            updated_at=activity_at,
        )
        runs = (run,)
    elif ci_status == "absent":
        runs = ()
    else:
        runs = (
            WorkflowRunEvidence(
                run_id=720,
                name="CI",
                status=ci_status,
                conclusion=ci_conclusion,
                attempt=1,
                head_sha=pr.head_sha,
                created_at=activity_at,
                updated_at=activity_at,
            ),
        )
    return replace(base, pull_requests=(pr,), workflow_runs=runs)


def evaluate_watchdogs(reader, finalizer, factory, *, now):
    return evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=reader,
        uow_factory=factory,
        max_parallel_dev_executions=1,
        pr_no_ci_after_seconds=60,
        ci_stall_after_seconds=60,
        auto_merge_grace_seconds=60,
        clock=lambda: now,
        finalizer=finalizer,
    )


def test_pr_without_ci_watchdog_dispatches_same_evidence_once(tmp_path):
    reader = MutableEvidenceReader(
        timed_evidence(ci_status="absent", activity_at="2026-10-06T12:00:00Z")
    )
    finalizer = FakeFinalizer()
    engine, factory = uow_factory(tmp_path)
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    try:
        first = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert len(first.dispatches) == 1
        assert "aucun workflow pull_request" in first.dispatches[0].prompt_text
        assert first.projection.items[0].github_watchdog is not None
        assert first.projection.items[0].github_watchdog.recovery_prepared is True

        second = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert second.dispatches == ()
    finally:
        engine.dispose()


def test_ci_stall_watchdog_resets_on_workflow_activity(tmp_path):
    reader = MutableEvidenceReader(
        timed_evidence(
            ci_status="in_progress",
            activity_at="2026-10-06T12:00:00Z",
        )
    )
    finalizer = FakeFinalizer()
    engine, factory = uow_factory(tmp_path)
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    try:
        first = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert len(first.dispatches) == 1
        assert "reste en cours sans progression" in first.dispatches[0].prompt_text

        current_run = reader.evidence.workflow_runs[0]
        reader.evidence = replace(
            reader.evidence,
            workflow_runs=(
                replace(
                    current_run,
                    attempt=2,
                    updated_at="2026-10-06T13:59:30Z",
                ),
            ),
        )
        refreshed = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert refreshed.dispatches == ()
        assert refreshed.projection.items[0].github_watchdog is not None
        assert refreshed.projection.items[0].github_watchdog.due is False
        with factory() as uow:
            stale_dispatch = uow.prompt_dispatches.get(first.dispatches[0].dispatch_id)
            assert stale_dispatch is not None
            assert stale_dispatch.status is PromptDispatchStatus.CANCELLED
    finally:
        engine.dispose()


def test_auto_merge_grace_falls_back_to_existing_finalizer(tmp_path):
    reader = MutableEvidenceReader(
        timed_evidence(
            auto_merge_enabled=True,
            activity_at="2026-10-06T12:00:00Z",
        )
    )

    def mark_merged():
        reader.evidence = evidence(merged=True)

    finalizer = FakeFinalizer(on_merge=mark_merged)
    engine, factory = uow_factory(tmp_path)
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    try:
        result = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]
        assert result.projection.items[0].execution.state is ExecutionState.ROADMAP_UPDATE_REQUIRED
    finally:
        engine.dispose()


def test_auto_merge_grace_reuses_dc071_branch_sync_when_branch_is_behind(tmp_path):
    reader = MutableEvidenceReader(
        timed_evidence(
            auto_merge_enabled=True,
            behind_by=1,
            activity_at="2026-10-06T12:00:00Z",
        )
    )
    finalizer = FakeFinalizer()
    engine, factory = uow_factory(tmp_path)
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    try:
        evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert finalizer.sync_calls == [(71, "head-1", "base-1")]
        assert finalizer.merge_calls == []
    finally:
        engine.dispose()


def test_auto_merge_grace_merge_blocker_is_persisted_and_not_bypassed(tmp_path):
    reader = MutableEvidenceReader(
        timed_evidence(
            auto_merge_enabled=True,
            activity_at="2026-10-06T12:00:00Z",
        )
    )
    finalizer = FakeFinalizer(
        merge_result=FinalizationMutationResult(
            FinalizationAttemptStatus.BLOCKED,
            error_code="MERGE_BLOCKED",
            message="Required approving review is missing",
        )
    )
    engine, factory = uow_factory(tmp_path)
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    try:
        first = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert first.projection.items[0].execution.state is ExecutionState.MERGE_BLOCKED
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]

        second = evaluate_watchdogs(reader, finalizer, factory, now=now)
        assert second.projection.items[0].execution.state is ExecutionState.MERGE_BLOCKED
        assert finalizer.merge_calls == [(71, "head-1", "base-1")]
    finally:
        engine.dispose()
