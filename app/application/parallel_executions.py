from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

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
from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    UnitOfWorkFactory,
    create_prompt_dispatch_in_uow,
)
from app.application.roadmaps import RoadmapIssue, RoadmapIssueReader, read_project_roadmap
from app.domain.execution import (
    ExecutionProjection,
    ExecutionState,
    NextAction,
    blocked_projection,
    derive_execution_projection,
)
from app.domain.project import Project
from app.domain.prompt_dispatch import (
    PromptDispatch,
    PromptDispatchRole,
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
    lease_owner_id: str = "devcockpit-process",
    clock: Callable[[], datetime] | None = None,
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
                global_inhibition="SCHEDULER_INVALID",
                now=now,
            )
            return ParallelDevExecutionEvaluation(projection=projection, dispatches=())

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
            global_inhibition=None,
            now=now,
        )

        remaining_starts = projection.available_capacity
        for item in projection.items:
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
            global_inhibition=None,
            now=now,
        )

    return ParallelDevExecutionEvaluation(
        projection=projection,
        dispatches=tuple(dispatches),
    )


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


def _stale_dev_due(
    project: Project,
    execution: ExecutionProjection,
    *,
    uow,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    if (
        stale_after_seconds <= 0
        or execution.state is not ExecutionState.DEVELOPING
        or execution.work_item is None
        or execution.branch is None
        or execution.branch.last_activity_at is None
    ):
        return False

    initial = uow.prompt_dispatches.get_by_idempotency_key(
        _initial_idempotency_key(project, execution.work_item)
    )
    if initial is None:
        return False

    delivery = uow.prompt_deliveries.get_by_dispatch_id(initial.dispatch_id)
    if delivery is None or not delivery.is_acknowledged or delivery.acknowledged_at is None:
        return False

    branch_activity = _parse_github_timestamp(execution.branch.last_activity_at)
    if branch_activity is None:
        return False

    reference = max(
        branch_activity,
        _as_utc(initial.created_at),
        _as_utc(delivery.acknowledged_at),
    )
    return (now - reference).total_seconds() >= stale_after_seconds


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


def _project_parallel_state(
    project: Project,
    *,
    issue: RoadmapIssue,
    scheduler: SchedulerProjection,
    snapshots: tuple[_CandidateSnapshot, ...],
    uow,
    max_parallel_dev_executions: int,
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
