from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from app.application.delivery_contexts import release_automation_allowed
from app.application.executions import (
    ExecutionEvidenceReader,
    ExecutionSourceError,
    _ci_red_idempotency_key,
    _initial_idempotency_key,
    _roadmap_reconcile_idempotency_key,
    _stale_dev_idempotency_key,
    build_ci_red_follow_up,
    build_initial_dev_prompt,
    build_roadmap_reconciliation_follow_up,
    build_stale_dev_follow_up,
)
from app.application.github_wait_watchdogs import (
    GitHubWaitWatchdog,
    GitHubWaitWatchdogKind,
    build_ci_stall_follow_up,
    build_pr_no_ci_follow_up,
    project_execution_github_wait_watchdog,
    watchdog_dispatch_key,
    watchdog_dispatch_prefix,
    with_recovery,
)
from app.application.interaction_summaries import InteractionSummary, read_interaction_summary
from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    UnitOfWorkFactory,
    create_prompt_dispatch_in_uow,
)
from app.application.pr_finalization import (
    PullRequestFinalizer,
    branch_sync_attempt_key,
    branch_sync_follow_up_key,
    build_branch_sync_blocked_follow_up,
    finalization_idempotency_key,
    operation_for,
    overlay_finalization_attempt,
)
from app.application.roadmaps import RoadmapIssue, RoadmapIssueReader, read_project_roadmap
from app.domain.execution import (
    ExecutionProjection,
    ExecutionState,
    NextAction,
    blocked_projection,
    derive_execution_projection,
)
from app.domain.pr_finalization import (
    FinalizationAttemptStatus,
    PullRequestFinalizationAttempt,
)
from app.domain.project import Project
from app.domain.prompt_dispatch import (
    PromptDispatch,
    PromptDispatchRole,
    PromptDispatchStatus,
    build_agent_session,
)
from app.domain.resource_lock import (
    ResourceLock,
    ResourceLockConflict,
    ResourceLockRequirement,
    ResourceLockState,
    lock_modes_compatible,
)
from app.domain.roadmap import WorkItemType
from app.domain.scheduler import (
    SchedulerItemProjection,
    SchedulerProjection,
    derive_scheduler_projection,
)


class DevExecutionSlotState(StrEnum):
    ACTIVE = "ACTIVE"
    SELECTED = "SELECTED"
    WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"
    WAITING_FOR_RESOURCE_LOCK = "WAITING_FOR_RESOURCE_LOCK"
    INHIBITED = "INHIBITED"


DevInteractionSummary = InteractionSummary


@dataclass(frozen=True, slots=True)
class DevWatchdogSummary:
    branch_last_activity_at: str
    threshold_seconds: float
    send_confirmed_at: datetime | None
    deadline_at: datetime | None
    stale_due: bool
    relaunch_prepared: bool


@dataclass(frozen=True, slots=True)
class DevExecutionItem:
    scheduler: SchedulerItemProjection
    execution: ExecutionProjection
    agent_session: str
    slot_state: DevExecutionSlotState
    active: bool
    waiting_for_capacity: bool
    waiting_for_resource_lock: bool
    required_locks: tuple[ResourceLockRequirement, ...] = ()
    lock_records: tuple[ResourceLock, ...] = ()
    lock_conflict: ResourceLockConflict | None = None
    lock_recovery_state: str | None = None
    inhibition_reason: str | None = None
    interaction: DevInteractionSummary | None = None
    watchdog: DevWatchdogSummary | None = None
    github_watchdog: GitHubWaitWatchdog | None = None


@dataclass(frozen=True, slots=True)
class ParallelDevExecutionProjection:
    project: Project
    issue: RoadmapIssue
    scheduler: SchedulerProjection
    max_parallel_dev_executions: int
    active_count: int
    items: tuple[DevExecutionItem, ...]

    @property
    def available_capacity(self) -> int:
        return max(0, self.max_parallel_dev_executions - self.active_count)


@dataclass(frozen=True, slots=True)
class ParallelDevExecutionEvaluation:
    projection: ParallelDevExecutionProjection
    dispatches: tuple[PromptDispatch, ...]


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    scheduler: SchedulerItemProjection
    execution: ExecutionProjection


def read_project_parallel_dev_executions(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
    max_parallel_dev_executions: int,
    dev_stale_after_seconds: float = 3600.0,
    pr_no_ci_after_seconds: float = 900.0,
    ci_stall_after_seconds: float = 1800.0,
    auto_merge_grace_seconds: float = 600.0,
    now: datetime | None = None,
) -> ParallelDevExecutionProjection:
    issue, scheduler, snapshots = _read_candidate_snapshots(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )
    current = _as_utc(now or datetime.now(timezone.utc))
    with uow_factory() as uow:
        fence = uow.roadmap_target_fences.snapshot(
            project.repository_full_name,
            project.roadmap_issue_number,
        )
        return _project_parallel_state(
            project,
            issue=issue,
            scheduler=scheduler,
            snapshots=snapshots,
            uow=uow,
            max_parallel_dev_executions=max_parallel_dev_executions,
            dev_stale_after_seconds=dev_stale_after_seconds,
            pr_no_ci_after_seconds=pr_no_ci_after_seconds,
            ci_stall_after_seconds=ci_stall_after_seconds,
            auto_merge_grace_seconds=auto_merge_grace_seconds,
            global_inhibition=(
                "ROADMAP_APPLICATION_ACTIVE"
                if fence.active_application_id is not None
                else None
            ),
            now=current,
        )


def evaluate_project_parallel_dev_executions(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
    max_parallel_dev_executions: int,
    resource_lock_lease_seconds: float = 900.0,
    dev_stale_after_seconds: float = 3600.0,
    pr_no_ci_after_seconds: float = 900.0,
    ci_stall_after_seconds: float = 1800.0,
    auto_merge_grace_seconds: float = 600.0,
    lease_owner_id: str = "devcockpit-process",
    clock: Callable[[], datetime] | None = None,
    finalizer: PullRequestFinalizer | None = None,
) -> ParallelDevExecutionEvaluation:
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    with uow_factory() as uow:
        initial_fence = uow.roadmap_target_fences.snapshot(
            project.repository_full_name,
            project.roadmap_issue_number,
        )

    issue, scheduler, snapshots = _read_candidate_snapshots(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )

    dispatches: list[PromptDispatch] = []
    with uow_factory() as uow:
        current_fence = uow.roadmap_target_fences.snapshot(
            project.repository_full_name,
            project.roadmap_issue_number,
        )
        fence_changed = (
            initial_fence.active_application_id is not None
            or current_fence.active_application_id is not None
            or current_fence.generation != initial_fence.generation
        )
        now = _as_utc(active_clock())
        if fence_changed:
            projection = _project_parallel_state(
                project,
                issue=issue,
                scheduler=scheduler,
                snapshots=snapshots,
                uow=uow,
                max_parallel_dev_executions=max_parallel_dev_executions,
                dev_stale_after_seconds=dev_stale_after_seconds,
                pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                ci_stall_after_seconds=ci_stall_after_seconds,
                auto_merge_grace_seconds=auto_merge_grace_seconds,
                global_inhibition="ROADMAP_APPLICATION_FENCE",
                now=now,
            )
            return ParallelDevExecutionEvaluation(projection=projection, dispatches=())

        if not scheduler.valid:
            projection = _project_parallel_state(
                project,
                issue=issue,
                scheduler=scheduler,
                snapshots=snapshots,
                uow=uow,
                max_parallel_dev_executions=max_parallel_dev_executions,
                dev_stale_after_seconds=dev_stale_after_seconds,
                pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                ci_stall_after_seconds=ci_stall_after_seconds,
                auto_merge_grace_seconds=auto_merge_grace_seconds,
                global_inhibition="SCHEDULER_INVALID",
                now=now,
            )
            return ParallelDevExecutionEvaluation(projection=projection, dispatches=())

        snapshots = _execute_deterministic_finalization_actions(
            project,
            snapshots=snapshots,
            evidence_reader=evidence_reader,
            uow=uow,
            finalizer=finalizer,
            now=now,
            pr_no_ci_after_seconds=pr_no_ci_after_seconds,
            ci_stall_after_seconds=ci_stall_after_seconds,
            auto_merge_grace_seconds=auto_merge_grace_seconds,
        )

        _reconcile_resource_locks(
            project,
            snapshots=snapshots,
            uow=uow,
            now=now,
            lease_seconds=resource_lock_lease_seconds,
            lease_owner_id=lease_owner_id,
        )
        _restore_active_execution_locks(
            project,
            snapshots=snapshots,
            uow=uow,
            now=now,
            lease_seconds=resource_lock_lease_seconds,
            lease_owner_id=lease_owner_id,
        )
        projection = _project_parallel_state(
            project,
            issue=issue,
            scheduler=scheduler,
            snapshots=snapshots,
            uow=uow,
            max_parallel_dev_executions=max_parallel_dev_executions,
            dev_stale_after_seconds=dev_stale_after_seconds,
            pr_no_ci_after_seconds=pr_no_ci_after_seconds,
            ci_stall_after_seconds=ci_stall_after_seconds,
            auto_merge_grace_seconds=auto_merge_grace_seconds,
            global_inhibition=None,
            now=now,
        )

        remaining_starts = projection.available_capacity
        for item in projection.items:
            _cancel_obsolete_github_watchdog_dispatches(
                project,
                item,
                uow=uow,
                now=now,
            )
            work_item = item.execution.work_item
            if work_item is None:
                continue

            if item.active:
                if (
                    item.execution.next_action is NextAction.FIX_CI
                    and not uow.handoffs.covers(
                        project.project_id,
                        work_item.key,
                        _ci_red_idempotency_key(project, item.execution),
                    )
                ):
                    dispatches.append(
                        create_prompt_dispatch_in_uow(
                            CreatePromptDispatchCommand(
                                project_id=project.project_id,
                                work_item_id=work_item.key,
                                role=PromptDispatchRole.DEV,
                                prompt_text=build_ci_red_follow_up(project, item.execution),
                                idempotency_key=_ci_red_idempotency_key(project, item.execution),
                            ),
                            uow=uow,
                        )
                    )
                elif item.execution.next_action is NextAction.RESOLVE_BRANCH_SYNC:
                    attempts = getattr(uow, "pr_finalization_attempts", None)
                    if attempts is not None:
                        try:
                            attempt = attempts.get_by_idempotency_key(
                                branch_sync_attempt_key(project, item.execution)
                            )
                        except ValueError:
                            attempt = None
                        if attempt is not None and attempt.requires_dev:
                            follow_up_key = branch_sync_follow_up_key(
                                project,
                                item.execution,
                                attempt,
                            )
                            if (
                                uow.prompt_dispatches.get_by_idempotency_key(
                                    follow_up_key
                                )
                                is None
                            ):
                                dispatches.append(
                                    create_prompt_dispatch_in_uow(
                                        CreatePromptDispatchCommand(
                                            project_id=project.project_id,
                                            work_item_id=work_item.key,
                                            role=PromptDispatchRole.DEV,
                                            prompt_text=build_branch_sync_blocked_follow_up(
                                                project,
                                                item.execution,
                                                attempt,
                                            ),
                                            idempotency_key=follow_up_key,
                                        ),
                                        uow=uow,
                                    )
                                )
                elif item.execution.next_action is NextAction.RECONCILE_ROADMAP:
                    dispatches.append(
                        create_prompt_dispatch_in_uow(
                            CreatePromptDispatchCommand(
                                project_id=project.project_id,
                                work_item_id=work_item.key,
                                role=PromptDispatchRole.DEV,
                                prompt_text=build_roadmap_reconciliation_follow_up(
                                    project,
                                    item.execution,
                                ),
                                idempotency_key=_roadmap_reconcile_idempotency_key(
                                    project,
                                    item.execution,
                                ),
                            ),
                            uow=uow,
                        )
                    )
                elif (
                    item.github_watchdog is not None
                    and item.github_watchdog.due
                    and item.github_watchdog.kind
                    in {
                        GitHubWaitWatchdogKind.PR_NO_CI,
                        GitHubWaitWatchdogKind.CI_STALLED,
                    }
                ):
                    watchdog_key = watchdog_dispatch_key(
                        project,
                        work_item.key,
                        item.github_watchdog,
                    )
                    if uow.prompt_dispatches.get_by_idempotency_key(watchdog_key) is None:
                        prompt_builder = (
                            build_pr_no_ci_follow_up
                            if item.github_watchdog.kind is GitHubWaitWatchdogKind.PR_NO_CI
                            else build_ci_stall_follow_up
                        )
                        dispatches.append(
                            create_prompt_dispatch_in_uow(
                                CreatePromptDispatchCommand(
                                    project_id=project.project_id,
                                    work_item_id=work_item.key,
                                    role=PromptDispatchRole.DEV,
                                    prompt_text=prompt_builder(
                                        project,
                                        item.execution,
                                        item.github_watchdog,
                                    ),
                                    idempotency_key=watchdog_key,
                                ),
                                uow=uow,
                            )
                        )
                elif _stale_dev_due(
                    project,
                    item.execution,
                    uow=uow,
                    now=now,
                    stale_after_seconds=dev_stale_after_seconds,
                ):
                    stale_key = _stale_dev_idempotency_key(
                        project,
                        item.execution,
                    )
                    if uow.prompt_dispatches.get_by_idempotency_key(stale_key) is None:
                        dispatches.append(
                            create_prompt_dispatch_in_uow(
                                CreatePromptDispatchCommand(
                                    project_id=project.project_id,
                                    work_item_id=work_item.key,
                                    role=PromptDispatchRole.DEV,
                                    prompt_text=build_stale_dev_follow_up(
                                        project,
                                        item.execution,
                                        inactivity_seconds=dev_stale_after_seconds,
                                    ),
                                    idempotency_key=stale_key,
                                ),
                                uow=uow,
                            )
                        )
                continue

            if (
                item.inhibition_reason is not None
                or item.execution.state is ExecutionState.BLOCKED
                or remaining_starts <= 0
            ):
                continue

            _, conflict = uow.resource_locks.acquire_many(
                project_id=project.project_id,
                work_item_id=work_item.key,
                agent_session=item.agent_session,
                lease_owner_id=lease_owner_id,
                requirements=item.required_locks,
                now=now,
                lease_seconds=resource_lock_lease_seconds,
            )
            if conflict is not None:
                continue

            initial_key = _initial_idempotency_key(project, work_item)
            existing_initial = uow.prompt_dispatches.get_by_idempotency_key(initial_key)
            if existing_initial is None:
                dispatches.append(
                    create_prompt_dispatch_in_uow(
                        CreatePromptDispatchCommand(
                            project_id=project.project_id,
                            work_item_id=work_item.key,
                            role=PromptDispatchRole.DEV,
                            prompt_text=build_initial_dev_prompt(project, work_item),
                            idempotency_key=initial_key,
                        ),
                        uow=uow,
                    )
                )
                # PromptDispatchRepository also runs with autoflush=False.
                # Make the dispatch visible to the next candidate and final projection.
                uow.flush()
            remaining_starts -= 1
        uow.commit()
        projection = _project_parallel_state(
            project,
            issue=issue,
            scheduler=scheduler,
            snapshots=snapshots,
            uow=uow,
            max_parallel_dev_executions=max_parallel_dev_executions,
            dev_stale_after_seconds=dev_stale_after_seconds,
            pr_no_ci_after_seconds=pr_no_ci_after_seconds,
            ci_stall_after_seconds=ci_stall_after_seconds,
            auto_merge_grace_seconds=auto_merge_grace_seconds,
            global_inhibition=None,
            now=now,
        )

    return ParallelDevExecutionEvaluation(
        projection=projection,
        dispatches=tuple(dispatches),
    )


def _execute_deterministic_finalization_actions(
    project: Project,
    *,
    snapshots: tuple[_CandidateSnapshot, ...],
    evidence_reader: ExecutionEvidenceReader,
    uow,
    finalizer: PullRequestFinalizer | None,
    now: datetime,
    pr_no_ci_after_seconds: float,
    ci_stall_after_seconds: float,
    auto_merge_grace_seconds: float,
) -> tuple[_CandidateSnapshot, ...]:
    attempts = getattr(uow, "pr_finalization_attempts", None)
    if attempts is None:
        return snapshots

    updated: list[_CandidateSnapshot] = []
    for snapshot in snapshots:
        execution = snapshot.execution
        watchdog = project_execution_github_wait_watchdog(
            execution,
            now=now,
            pr_no_ci_after_seconds=pr_no_ci_after_seconds,
            ci_stall_after_seconds=ci_stall_after_seconds,
            auto_merge_grace_seconds=auto_merge_grace_seconds,
        )
        if (
            watchdog is not None
            and watchdog.kind is GitHubWaitWatchdogKind.AUTO_MERGE_GRACE
            and watchdog.due
            and execution.next_action is NextAction.WAIT_AUTO_MERGE
        ):
            execution = replace(execution, next_action=NextAction.MERGE_PR)
        execution = overlay_finalization_attempt(
            project,
            execution,
            attempts=attempts,
        )
        if (
            finalizer is None
            or execution.next_action not in {NextAction.SYNC_BRANCH, NextAction.MERGE_PR}
            or execution.work_item is None
            or execution.pull_request is None
            or uow.handoffs.active(project.project_id, execution.work_item.key)
        ):
            updated.append(_CandidateSnapshot(snapshot.scheduler, execution))
            continue

        key = finalization_idempotency_key(project, execution)
        existing = attempts.get_by_idempotency_key(key)
        if existing is not None:
            updated.append(
                _CandidateSnapshot(
                    snapshot.scheduler,
                    overlay_finalization_attempt(
                        project,
                        execution,
                        attempts=attempts,
                    ),
                )
            )
            continue

        pull_request = execution.pull_request
        claim = PullRequestFinalizationAttempt.claim(
            idempotency_key=key,
            project_id=project.project_id,
            work_item_id=execution.work_item.key,
            pr_number=pull_request.number,
            operation=operation_for(execution),
            expected_head_sha=pull_request.head_sha,
            base_sha=pull_request.base_sha,
        )
        attempts.add(claim)
        uow.commit()

        context = project.delivery_context_for(execution.work_item.key)
        target_guard = {"delivery_context": context} if context is not None else {}
        if execution.next_action is NextAction.SYNC_BRANCH:
            if pull_request.base_sha is None:
                result = None
            else:
                result = finalizer.sync_branch(
                    project,
                    pr_number=pull_request.number,
                    expected_head_sha=pull_request.head_sha,
                    expected_base_sha=pull_request.base_sha,
                    **target_guard,
                )
        else:
            result = finalizer.merge_pull_request(
                project,
                pr_number=pull_request.number,
                expected_head_sha=pull_request.head_sha,
                expected_base_sha=pull_request.base_sha,
                **target_guard,
            )

        if result is None:
            completed = claim.complete(
                status=FinalizationAttemptStatus.BLOCKED,
                error_code="BASE_SHA_NOT_OBSERVED",
                message="Branch synchronization requires an observed base SHA.",
            )
        else:
            completed = claim.complete(
                status=result.status,
                error_code=result.error_code,
                message=result.message,
                requires_dev=result.requires_dev,
                resulting_head_sha=result.resulting_head_sha,
            )
        attempts.save(completed)
        uow.commit()

        refreshed = execution
        if result is not None and result.status.value in {"SUCCEEDED", "STALE"}:
            try:
                refreshed = derive_execution_projection(
                    execution.work_item,
                    evidence_reader.read(project, execution.work_item),
                )
            except ExecutionSourceError:
                refreshed = execution
        refreshed = overlay_finalization_attempt(
            project,
            refreshed,
            attempts=attempts,
        )
        updated.append(_CandidateSnapshot(snapshot.scheduler, refreshed))

    return tuple(updated)


def _read_candidate_snapshots(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
) -> tuple[RoadmapIssue, SchedulerProjection, tuple[_CandidateSnapshot, ...]]:
    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    scheduler = derive_scheduler_projection(roadmap.pipeline)
    snapshots: list[_CandidateSnapshot] = []

    if not scheduler.valid:
        return roadmap.issue, scheduler, ()

    for scheduler_item in scheduler.items:
        if (
            not scheduler_item.executable
            or scheduler_item.work_item.type is not WorkItemType.WORK
            or scheduler_item.expected_role != "DEV"
        ):
            continue
        work_item = scheduler_item.work_item
        if not release_automation_allowed(project.delivery_context_for(work_item.key)):
            snapshots.append(_CandidateSnapshot(
                scheduler_item,
                blocked_projection(
                    work_item=work_item,
                    code="RELEASE_AUTOMATION_DISABLED",
                    message="Non-NORMAL delivery requires DC-075B authorization and GitHub revalidation.",
                ),
            ))
            continue
        try:
            evidence = evidence_reader.read(project, work_item)
        except ExecutionSourceError as exc:
            execution = blocked_projection(
                work_item=work_item,
                code=exc.code,
                message="GitHub execution evidence is unavailable.",
            )
        else:
            execution = derive_execution_projection(work_item, evidence)
        snapshots.append(_CandidateSnapshot(scheduler_item, execution))

    return roadmap.issue, scheduler, tuple(snapshots)


def _stale_dev_reference(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
) -> datetime | None:
    if (
        execution.state is not ExecutionState.DEVELOPING
        or execution.work_item is None
        or execution.branch is None
        or execution.branch.last_activity_at is None
    ):
        return None

    branch_activity = _parse_github_timestamp(execution.branch.last_activity_at)
    if branch_activity is None:
        return None

    initial = uow.prompt_dispatches.get_by_idempotency_key(
        _initial_idempotency_key(project, execution.work_item)
    )
    if initial is None:
        # GitHub already proves work exists on a branch strictly matched to this
        # WorkItem. After a local DB/reset loss, use branch inactivity as the
        # restart-safe lower bound for a same-session orphan recovery.
        return branch_activity

    initial_created_at = _as_utc(initial.created_at)
    delivery = uow.prompt_deliveries.get_by_dispatch_id(initial.dispatch_id)
    if delivery is None:
        # Local transport evidence is incomplete, but an existing GitHub branch
        # proves the DEV execution already started. Do not immediately relaunch:
        # respect the later of branch activity and the local dispatch creation.
        return max(branch_activity, initial_created_at)

    send_repository = getattr(uow, "chatgpt_prompt_sends", None)
    if send_repository is not None:
        from app.domain.chatgpt_prompt_send import ChatGptPromptSendState

        prompt_send = send_repository.get(delivery.delivery_id)
        if prompt_send is None:
            # Same restart/rebuild recovery rule as above: absence of local send
            # evidence may not permanently strand an authoritative GitHub branch.
            return max(branch_activity, initial_created_at)
        if (
            prompt_send.state is not ChatGptPromptSendState.SENT_CONFIRMED
            or prompt_send.confirmed_at is None
        ):
            # Explicit browser send state remains authoritative for safety.
            # In particular SEND_ARMED/AMBIGUOUS must never trigger another prompt.
            return None
        send_lower_bound = prompt_send.confirmed_at
    else:
        # Compatibility for isolated legacy test doubles. Production UoWs always
        # expose chatgpt_prompt_sends after DC-063B.
        if not delivery.is_acknowledged or delivery.acknowledged_at is None:
            return None
        send_lower_bound = delivery.acknowledged_at

    return max(
        branch_activity,
        initial_created_at,
        _as_utc(send_lower_bound),
    )


def _stale_dev_due(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    if stale_after_seconds <= 0:
        return False
    reference = _stale_dev_reference(project, execution, uow=uow)
    return (
        reference is not None
        and (now - reference).total_seconds() >= stale_after_seconds
    )

def _parse_github_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _as_utc(parsed)


def _reconcile_resource_locks(
    project: Project,
    *,
    snapshots: tuple[_CandidateSnapshot, ...],
    uow,
    now: datetime,
    lease_seconds: float,
    lease_owner_id: str,
) -> None:
    by_work_item = {
        snapshot.scheduler.work_item.key: snapshot
        for snapshot in snapshots
    }
    for lock in uow.resource_locks.list_active_for_project(project.project_id):
        snapshot = by_work_item.get(lock.work_item_id)
        if snapshot is None:
            uow.resource_locks.save(
                lock.release(
                    now=now,
                    reason="WORK_ITEM_NO_LONGER_EXECUTABLE",
                )
            )
            continue

        github_execution_active = snapshot.execution.state not in {
            ExecutionState.READY,
            ExecutionState.BLOCKED,
        }
        if lock.expired(now):
            if github_execution_active:
                uow.resource_locks.save(
                    lock.renew(
                        lease_owner_id=lease_owner_id,
                        now=now,
                        lease_seconds=lease_seconds,
                    )
                )
            elif snapshot.execution.state is ExecutionState.BLOCKED:
                # Ambiguous/unavailable GitHub evidence is not proof that the
                # execution ended. Keep the expired ACTIVE lock conservative
                # until evidence becomes determinate.
                continue
            else:
                uow.resource_locks.save(
                    lock.mark_stale(
                        now=now,
                        reason="LEASE_EXPIRED_WITHOUT_ACTIVE_GITHUB_EVIDENCE",
                    )
                )
            continue

        if lock.lease_owner_id == lease_owner_id or github_execution_active:
            uow.resource_locks.save(
                lock.renew(
                    lease_owner_id=lease_owner_id,
                    now=now,
                    lease_seconds=lease_seconds,
                )
            )
    uow.flush()


def _restore_active_execution_locks(
    project: Project,
    *,
    snapshots: tuple[_CandidateSnapshot, ...],
    uow,
    now: datetime,
    lease_seconds: float,
    lease_owner_id: str,
) -> None:
    for snapshot in snapshots:
        if snapshot.execution.state in {ExecutionState.READY, ExecutionState.BLOCKED}:
            continue
        work_item = snapshot.scheduler.work_item
        requirements = project.resource_lock_requirements_for(work_item.key)
        if not requirements:
            continue
        owner_records = uow.resource_locks.list_for_owner(
            project.project_id,
            work_item.key,
        )
        if _holds_required_locks(requirements, owner_records=owner_records):
            continue
        uow.resource_locks.acquire_many(
            project_id=project.project_id,
            work_item_id=work_item.key,
            agent_session=build_agent_session(
                project.project_id,
                PromptDispatchRole.DEV,
                work_item.key,
            ),
            lease_owner_id=lease_owner_id,
            requirements=requirements,
            now=now,
            lease_seconds=lease_seconds,
        )



def _cancel_obsolete_github_watchdog_dispatches(
    project: Project,
    item: DevExecutionItem,
    *,
    uow,
    now: datetime,
) -> None:
    work_item = item.execution.work_item
    if work_item is None:
        return
    repository = getattr(uow, "prompt_dispatches", None)
    list_for_work_item = getattr(repository, "list_for_work_item", None)
    save = getattr(repository, "save", None)
    if list_for_work_item is None or save is None:
        return

    current_key = None
    watchdog = item.github_watchdog
    if (
        watchdog is not None
        and watchdog.due
        and watchdog.kind
        in {
            GitHubWaitWatchdogKind.PR_NO_CI,
            GitHubWaitWatchdogKind.CI_STALLED,
        }
    ):
        current_key = watchdog_dispatch_key(project, work_item.key, watchdog)

    prefix = watchdog_dispatch_prefix(project, work_item.key)
    for dispatch in list_for_work_item(project.project_id, work_item.key):
        if (
            dispatch.status is not PromptDispatchStatus.PREPARED
            or not dispatch.idempotency_key.startswith(prefix)
            or dispatch.idempotency_key == current_key
        ):
            continue
        delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
        if delivery is not None and delivery.is_acknowledged:
            # The extension may already own this prompt. There is deliberately no
            # remote-revocation protocol; the prompt itself revalidates GitHub and
            # must fail stale. Only unsent/unacknowledged local work is cancelled.
            continue
        dispatch.cancel(now=max(now, dispatch.updated_at))
        save(dispatch)


def _selected_dev_dispatch(project: Project, execution: ExecutionProjection, *, uow):
    work_item = execution.work_item
    if work_item is None:
        return None

    candidate_keys: list[str] = []
    if execution.state is ExecutionState.CI_RED:
        try:
            candidate_keys.append(_ci_red_idempotency_key(project, execution))
        except ValueError:
            pass
    elif execution.state is ExecutionState.BRANCH_SYNC_BLOCKED:
        attempts = getattr(uow, "pr_finalization_attempts", None)
        if attempts is not None:
            try:
                attempt = attempts.get_by_idempotency_key(
                    branch_sync_attempt_key(project, execution)
                )
            except ValueError:
                attempt = None
            if attempt is not None and attempt.requires_dev:
                try:
                    candidate_keys.append(
                        branch_sync_follow_up_key(project, execution, attempt)
                    )
                except ValueError:
                    pass
    elif execution.state is ExecutionState.ROADMAP_UPDATE_REQUIRED:
        try:
            candidate_keys.append(_roadmap_reconcile_idempotency_key(project, execution))
        except ValueError:
            pass
    elif execution.state is ExecutionState.DEVELOPING:
        try:
            candidate_keys.append(_stale_dev_idempotency_key(project, execution))
        except ValueError:
            pass
    candidate_keys.append(_initial_idempotency_key(project, work_item))

    for key in candidate_keys:
        dispatch = uow.prompt_dispatches.get_by_idempotency_key(key)
        if dispatch is not None:
            return dispatch
    return None


def _interaction_summary(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
) -> DevInteractionSummary | None:
    dispatch = _selected_dev_dispatch(project, execution, uow=uow)
    if dispatch is None:
        return None
    return read_interaction_summary(dispatch, uow=uow)


def _watchdog_summary(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
    now: datetime,
    stale_after_seconds: float,
) -> DevWatchdogSummary | None:
    branch = execution.branch
    if (
        execution.state is not ExecutionState.DEVELOPING
        or branch is None
        or branch.last_activity_at is None
    ):
        return None

    initial = (
        uow.prompt_dispatches.get_by_idempotency_key(
            _initial_idempotency_key(project, execution.work_item)
        )
        if execution.work_item is not None
        else None
    )
    delivery = (
        uow.prompt_deliveries.get_by_dispatch_id(initial.dispatch_id)
        if initial is not None
        else None
    )
    send_repository = getattr(uow, "chatgpt_prompt_sends", None)
    prompt_send = (
        send_repository.get(delivery.delivery_id)
        if send_repository is not None and delivery is not None
        else None
    )
    try:
        stale_key = _stale_dev_idempotency_key(project, execution)
    except ValueError:
        relaunch_prepared = False
    else:
        relaunch_prepared = (
            uow.prompt_dispatches.get_by_idempotency_key(stale_key) is not None
        )

    reference = _stale_dev_reference(project, execution, uow=uow)
    deadline_at = (
        reference + timedelta(seconds=stale_after_seconds)
        if reference is not None and stale_after_seconds > 0
        else None
    )

    return DevWatchdogSummary(
        branch_last_activity_at=branch.last_activity_at,
        threshold_seconds=stale_after_seconds,
        send_confirmed_at=prompt_send.confirmed_at if prompt_send is not None else None,
        deadline_at=deadline_at,
        stale_due=_stale_dev_due(
            project,
            execution,
            uow=uow,
            now=now,
            stale_after_seconds=stale_after_seconds,
        ),
        relaunch_prepared=relaunch_prepared,
    )

def _github_wait_watchdog_summary(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
    now: datetime,
    pr_no_ci_after_seconds: float,
    ci_stall_after_seconds: float,
    auto_merge_grace_seconds: float,
) -> GitHubWaitWatchdog | None:
    watchdog = project_execution_github_wait_watchdog(
        execution,
        now=now,
        pr_no_ci_after_seconds=pr_no_ci_after_seconds,
        ci_stall_after_seconds=ci_stall_after_seconds,
        auto_merge_grace_seconds=auto_merge_grace_seconds,
    )
    if watchdog is None or execution.work_item is None:
        return watchdog

    if watchdog.kind in {
        GitHubWaitWatchdogKind.PR_NO_CI,
        GitHubWaitWatchdogKind.CI_STALLED,
    }:
        prepared = (
            uow.prompt_dispatches.get_by_idempotency_key(
                watchdog_dispatch_key(project, execution.work_item.key, watchdog)
            )
            is not None
        )
        return with_recovery(
            watchdog,
            prepared=prepared,
            state=(
                "DEV_RELAUNCH_PREPARED"
                if prepared
                else "DEV_RELAUNCH_DUE"
                if watchdog.due
                else "WAITING"
            ),
        )

    attempts = getattr(uow, "pr_finalization_attempts", None)
    attempt = None
    if attempts is not None and execution.pull_request is not None:
        recovery_projection = replace(execution, next_action=NextAction.MERGE_PR)
        try:
            key = finalization_idempotency_key(project, recovery_projection)
        except ValueError:
            key = None
        if key is not None:
            attempt = attempts.get_by_idempotency_key(key)
    if attempt is None:
        return with_recovery(
            watchdog,
            prepared=False,
            state="FINALIZER_DUE" if watchdog.due else "WAITING_FOR_GITHUB",
        )
    return with_recovery(
        watchdog,
        prepared=True,
        state=f"FINALIZER_{attempt.status.value}",
    )


def _project_parallel_state(
    project: Project,
    *,
    issue: RoadmapIssue,
    scheduler: SchedulerProjection,
    snapshots: tuple[_CandidateSnapshot, ...],
    uow,
    max_parallel_dev_executions: int,
    dev_stale_after_seconds: float,
    pr_no_ci_after_seconds: float,
    ci_stall_after_seconds: float,
    auto_merge_grace_seconds: float,
    global_inhibition: str | None,
    now: datetime,
) -> ParallelDevExecutionProjection:
    active_locks = uow.resource_locks.list_active_for_project(project.project_id)
    lock_records = uow.resource_locks.list_for_project(project.project_id)
    records_by_owner: dict[str, list[ResourceLock]] = {}
    for lock in lock_records:
        records_by_owner.setdefault(lock.work_item_id, []).append(lock)

    state: list[
        tuple[
            _CandidateSnapshot,
            bool,
            str | None,
            tuple[ResourceLockRequirement, ...],
            tuple[ResourceLock, ...],
            ResourceLockConflict | None,
            str | None,
        ]
    ] = []
    for snapshot in snapshots:
        work_item = snapshot.scheduler.work_item
        requirements = project.resource_lock_requirements_for(work_item.key)
        owner_records = tuple(records_by_owner.get(work_item.key, ()))
        has_initial_dispatch = (
            uow.prompt_dispatches.get_by_idempotency_key(
                _initial_idempotency_key(project, work_item)
            )
            is not None
        )
        dispatch_proves_active = has_initial_dispatch and (
            not requirements
            or _holds_required_locks(
                requirements,
                owner_records=owner_records,
            )
        )
        active = (
            dispatch_proves_active
            or snapshot.execution.state
            not in {ExecutionState.READY, ExecutionState.BLOCKED}
        )
        inhibition_reason = global_inhibition
        if inhibition_reason is None and uow.handoffs.active(
            project.project_id,
            work_item.key,
        ):
            inhibition_reason = "HANDOFF_ACTIVE"

        conflict = _find_resource_lock_conflict(
            work_item.key,
            requirements=requirements,
            active_locks=active_locks,
        )
        recovery_state = _lock_recovery_state(owner_records, now=now)
        state.append(
            (
                snapshot,
                active,
                inhibition_reason,
                requirements,
                owner_records,
                conflict,
                recovery_state,
            )
        )

    active_count = sum(1 for _, active, *_ in state if active)
    remaining = max(0, max_parallel_dev_executions - active_count)
    items: list[DevExecutionItem] = []

    for (
        snapshot,
        active,
        inhibition_reason,
        requirements,
        owner_records,
        conflict,
        recovery_state,
    ) in state:
        execution = snapshot.execution
        attempts = getattr(uow, "pr_finalization_attempts", None)
        if attempts is not None:
            execution = overlay_finalization_attempt(
                project,
                execution,
                attempts=attempts,
            )
        waiting_for_lock = False
        if active:
            slot_state = DevExecutionSlotState.ACTIVE
            waiting_for_capacity = False
        elif inhibition_reason is not None or execution.state is ExecutionState.BLOCKED:
            slot_state = DevExecutionSlotState.INHIBITED
            waiting_for_capacity = False
        elif remaining <= 0:
            slot_state = DevExecutionSlotState.WAITING_FOR_CAPACITY
            waiting_for_capacity = True
        elif conflict is not None:
            slot_state = DevExecutionSlotState.WAITING_FOR_RESOURCE_LOCK
            waiting_for_capacity = False
            waiting_for_lock = True
            inhibition_reason = "RESOURCE_LOCK_CONFLICT"
        else:
            slot_state = DevExecutionSlotState.SELECTED
            waiting_for_capacity = False
            remaining -= 1

        items.append(
            DevExecutionItem(
                scheduler=snapshot.scheduler,
                execution=execution,
                agent_session=build_agent_session(
                    project.project_id,
                    PromptDispatchRole.DEV,
                    snapshot.scheduler.work_item.key,
                ),
                slot_state=slot_state,
                active=active,
                waiting_for_capacity=waiting_for_capacity,
                waiting_for_resource_lock=waiting_for_lock,
                required_locks=requirements,
                lock_records=owner_records,
                lock_conflict=conflict,
                lock_recovery_state=recovery_state,
                inhibition_reason=inhibition_reason,
                interaction=_interaction_summary(
                    project,
                    execution,
                    uow=uow,
                ),
                watchdog=_watchdog_summary(
                    project,
                    execution,
                    uow=uow,
                    now=now,
                    stale_after_seconds=dev_stale_after_seconds,
                ),
                github_watchdog=_github_wait_watchdog_summary(
                    project,
                    execution,
                    uow=uow,
                    now=now,
                    pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                    ci_stall_after_seconds=ci_stall_after_seconds,
                    auto_merge_grace_seconds=auto_merge_grace_seconds,
                ),
            )
        )

    return ParallelDevExecutionProjection(
        project=project,
        issue=issue,
        scheduler=scheduler,
        max_parallel_dev_executions=max_parallel_dev_executions,
        active_count=active_count,
        items=tuple(items),
    )


def _holds_required_locks(
    requirements: tuple[ResourceLockRequirement, ...],
    *,
    owner_records: tuple[ResourceLock, ...],
) -> bool:
    active_by_surface = {
        lock.surface.key: lock
        for lock in owner_records
        if lock.state is ResourceLockState.ACTIVE
    }
    return all(
        requirement.surface.key in active_by_surface
        and active_by_surface[requirement.surface.key].mode is requirement.mode
        for requirement in requirements
    )


def _find_resource_lock_conflict(
    work_item_id: str,
    *,
    requirements: tuple[ResourceLockRequirement, ...],
    active_locks: tuple[ResourceLock, ...],
) -> ResourceLockConflict | None:
    for requirement in requirements:
        for holder in active_locks:
            if holder.work_item_id == work_item_id:
                continue
            if holder.surface != requirement.surface:
                continue
            if lock_modes_compatible(requirement.mode, holder.mode):
                continue
            return ResourceLockConflict(
                surface=requirement.surface,
                requested_mode=requirement.mode,
                holder_work_item_id=holder.work_item_id,
                holder_agent_session=holder.agent_session,
                holder_mode=holder.mode,
                holder_state=holder.state,
            )
    return None


def _lock_recovery_state(
    records: tuple[ResourceLock, ...],
    *,
    now: datetime,
) -> str | None:
    if any(lock.state is ResourceLockState.ACTIVE and lock.expired(now) for lock in records):
        return "LEASE_EXPIRED_PENDING_RECONCILIATION"
    if any(lock.state is ResourceLockState.STALE for lock in records):
        return "STALE_RECOVERED"
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
