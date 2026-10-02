from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.roadmap import (
    PipelineDiagnostic,
    PipelineParseResult,
    WorkItem,
    WorkItemStatus,
    WorkItemType,
)


class SchedulerState(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    HISTORICAL = "HISTORICAL"


class SchedulerReason(StrEnum):
    READY = "READY"
    ALREADY_DONE = "ALREADY_DONE"
    SUPERSEDED = "SUPERSEDED"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    CANONICAL_STATUS_BLOCKED = "CANONICAL_STATUS_BLOCKED"
    INVALID_ROADMAP = "INVALID_ROADMAP"


class SchedulerNextAction(StrEnum):
    START_DEV = "START_DEV"
    START_ARCH = "START_ARCH"
    PROMOTE_READY = "PROMOTE_READY"
    WAIT_FOR_DEPENDENCIES = "WAIT_FOR_DEPENDENCIES"
    RESOLVE_ROADMAP = "RESOLVE_ROADMAP"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class SchedulerDiagnostic:
    code: str
    message: str
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class SchedulerItemProjection:
    work_item: WorkItem
    dependencies: tuple[str, ...]
    unsatisfied_dependencies: tuple[str, ...]
    executable: bool
    state: SchedulerState
    reason: SchedulerReason
    expected_role: str | None
    next_action: SchedulerNextAction


@dataclass(frozen=True, slots=True)
class SchedulerProjection:
    valid: bool
    pipeline_version: int | None
    items: tuple[SchedulerItemProjection, ...]
    executable_candidates: tuple[str, ...]
    selected_candidate: str | None
    diagnostics: tuple[SchedulerDiagnostic, ...]


def derive_scheduler_projection(pipeline: PipelineParseResult) -> SchedulerProjection:
    if not pipeline.valid:
        return SchedulerProjection(
            valid=False,
            pipeline_version=pipeline.version,
            items=tuple(
                _invalid_item(item)
                for item in pipeline.work_items
            ),
            executable_candidates=(),
            selected_candidate=None,
            diagnostics=tuple(_scheduler_diagnostic(item) for item in pipeline.diagnostics),
        )

    by_key = {item.key: item for item in pipeline.work_items}
    items = tuple(_project_item(item, by_key) for item in pipeline.work_items)
    candidates = tuple(item.work_item.key for item in items if item.executable)
    selected = candidates[0] if len(candidates) == 1 else None
    return SchedulerProjection(
        valid=True,
        pipeline_version=pipeline.version,
        items=items,
        executable_candidates=candidates,
        selected_candidate=selected,
        diagnostics=(),
    )


def _project_item(
    item: WorkItem,
    by_key: dict[str, WorkItem],
) -> SchedulerItemProjection:
    unsatisfied = tuple(
        dependency
        for dependency in item.depends_on
        if by_key[dependency].status is not WorkItemStatus.DONE
    )

    if item.status is WorkItemStatus.DONE:
        return SchedulerItemProjection(
            work_item=item,
            dependencies=item.depends_on,
            unsatisfied_dependencies=(),
            executable=False,
            state=SchedulerState.COMPLETED,
            reason=SchedulerReason.ALREADY_DONE,
            expected_role=None,
            next_action=SchedulerNextAction.NONE,
        )

    if item.status is WorkItemStatus.SUPERSEDED:
        return SchedulerItemProjection(
            work_item=item,
            dependencies=item.depends_on,
            unsatisfied_dependencies=unsatisfied,
            executable=False,
            state=SchedulerState.HISTORICAL,
            reason=SchedulerReason.SUPERSEDED,
            expected_role=None,
            next_action=SchedulerNextAction.NONE,
        )

    expected_role = "ARCH" if item.type is WorkItemType.ARCHITECTURE_GATE else "DEV"

    if unsatisfied:
        return SchedulerItemProjection(
            work_item=item,
            dependencies=item.depends_on,
            unsatisfied_dependencies=unsatisfied,
            executable=False,
            state=SchedulerState.BLOCKED,
            reason=SchedulerReason.WAITING_FOR_DEPENDENCY,
            expected_role=expected_role,
            next_action=SchedulerNextAction.WAIT_FOR_DEPENDENCIES,
        )

    if item.status is WorkItemStatus.BLOCKED:
        return SchedulerItemProjection(
            work_item=item,
            dependencies=item.depends_on,
            unsatisfied_dependencies=(),
            executable=False,
            state=SchedulerState.BLOCKED,
            reason=SchedulerReason.CANONICAL_STATUS_BLOCKED,
            expected_role="PO",
            next_action=SchedulerNextAction.PROMOTE_READY,
        )

    return SchedulerItemProjection(
        work_item=item,
        dependencies=item.depends_on,
        unsatisfied_dependencies=(),
        executable=True,
        state=SchedulerState.EXECUTABLE,
        reason=SchedulerReason.READY,
        expected_role=expected_role,
        next_action=(
            SchedulerNextAction.START_ARCH
            if item.type is WorkItemType.ARCHITECTURE_GATE
            else SchedulerNextAction.START_DEV
        ),
    )


def _invalid_item(item: WorkItem) -> SchedulerItemProjection:
    return SchedulerItemProjection(
        work_item=item,
        dependencies=item.depends_on,
        unsatisfied_dependencies=item.depends_on,
        executable=False,
        state=SchedulerState.BLOCKED,
        reason=SchedulerReason.INVALID_ROADMAP,
        expected_role=None,
        next_action=SchedulerNextAction.RESOLVE_ROADMAP,
    )


def _scheduler_diagnostic(diagnostic: PipelineDiagnostic) -> SchedulerDiagnostic:
    return SchedulerDiagnostic(
        code=diagnostic.code,
        message=diagnostic.message,
        line_number=diagnostic.line_number,
    )
