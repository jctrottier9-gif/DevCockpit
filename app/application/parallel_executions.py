from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.executions import (
    ExecutionEvidenceReader,
    ExecutionSourceError,
    _ci_red_idempotency_key,
    _initial_idempotency_key,
    build_ci_red_follow_up,
    build_initial_dev_prompt,
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
    INHIBITED = "INHIBITED"


@dataclass(frozen=True, slots=True)
class DevExecutionItem:
    scheduler: SchedulerItemProjection
    execution: ExecutionProjection
    agent_session: str
    slot_state: DevExecutionSlotState
    active: bool
    waiting_for_capacity: bool
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
) -> ParallelDevExecutionProjection:
    issue, scheduler, snapshots = _read_candidate_snapshots(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )
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
        )


def evaluate_project_parallel_dev_executions(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
    max_parallel_dev_executions: int,
) -> ParallelDevExecutionEvaluation:
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
        projection = _project_parallel_state(
            project,
            issue=issue,
            scheduler=scheduler,
            snapshots=snapshots,
            uow=uow,
            max_parallel_dev_executions=max_parallel_dev_executions,
            global_inhibition=(
                "ROADMAP_APPLICATION_FENCE"
                if fence_changed
                else None
            ),
        )
        if fence_changed:
            return ParallelDevExecutionEvaluation(projection=projection, dispatches=())

        for item in projection.items:
            work_item = item.execution.work_item
            if work_item is None or item.inhibition_reason is not None:
                continue
            if item.slot_state is DevExecutionSlotState.SELECTED:
                dispatches.append(
                    create_prompt_dispatch_in_uow(
                        CreatePromptDispatchCommand(
                            project_id=project.project_id,
                            work_item_id=work_item.key,
                            role=PromptDispatchRole.DEV,
                            prompt_text=build_initial_dev_prompt(project, work_item),
                            idempotency_key=_initial_idempotency_key(project, work_item),
                        ),
                        uow=uow,
                    )
                )
            elif (
                item.active
                and item.execution.next_action is NextAction.FIX_CI
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
        projection = _project_parallel_state(
            project,
            issue=issue,
            scheduler=scheduler,
            snapshots=snapshots,
            uow=uow,
            max_parallel_dev_executions=max_parallel_dev_executions,
            global_inhibition=None,
        )
        uow.commit()

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


def _project_parallel_state(
    project: Project,
    *,
    issue: RoadmapIssue,
    scheduler: SchedulerProjection,
    snapshots: tuple[_CandidateSnapshot, ...],
    uow,
    max_parallel_dev_executions: int,
    global_inhibition: str | None,
) -> ParallelDevExecutionProjection:
    state: list[tuple[_CandidateSnapshot, bool, str | None]] = []
    for snapshot in snapshots:
        work_item = snapshot.scheduler.work_item
        has_initial_dispatch = (
            uow.prompt_dispatches.get_by_idempotency_key(
                _initial_idempotency_key(project, work_item)
            )
            is not None
        )
        active = (
            has_initial_dispatch
            or snapshot.execution.state
            not in {ExecutionState.READY, ExecutionState.BLOCKED}
        )
        inhibition_reason = global_inhibition
        if inhibition_reason is None and uow.handoffs.active(
            project.project_id,
            work_item.key,
        ):
            inhibition_reason = "HANDOFF_ACTIVE"
        state.append((snapshot, active, inhibition_reason))

    active_count = sum(1 for _, active, _ in state if active)
    remaining = max(0, max_parallel_dev_executions - active_count)
    items: list[DevExecutionItem] = []

    for snapshot, active, inhibition_reason in state:
        execution = snapshot.execution
        if active:
            slot_state = DevExecutionSlotState.ACTIVE
            waiting = False
        elif inhibition_reason is not None or execution.state is ExecutionState.BLOCKED:
            slot_state = DevExecutionSlotState.INHIBITED
            waiting = False
        elif remaining > 0:
            slot_state = DevExecutionSlotState.SELECTED
            waiting = False
            remaining -= 1
        else:
            slot_state = DevExecutionSlotState.WAITING_FOR_CAPACITY
            waiting = True

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
                waiting_for_capacity=waiting,
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
