from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from app.application.attention import AttentionProjection, read_project_attention
from app.application.executions import ExecutionEvidenceReader
from app.application.parallel_executions import (
    ParallelDevExecutionProjection,
    read_project_parallel_dev_executions,
)
from app.application.prompt_dispatches import UnitOfWorkFactory
from app.application.roadmaps import RoadmapIssueReader, RoadmapSourceError, read_project_roadmap
from app.domain.project import Project
from app.domain.roadmap import WorkItem, WorkItemStatus
from app.domain.scheduler import SchedulerItemProjection, derive_scheduler_projection


@dataclass(frozen=True, slots=True)
class CockpitDiagnostic:
    code: str
    message: str
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class CockpitSource:
    status: str
    code: str | None = None
    updated_at: str | None = None
    revision: str | None = None
    diagnostics: tuple[CockpitDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class CockpitHorizonItem:
    key: str
    title: str
    type: str
    status: str
    lane: str
    scheduler_state: str | None
    expected_role: str | None
    next_action: str | None


@dataclass(frozen=True, slots=True)
class CockpitRoleSummary:
    role: str
    label: str
    state: str
    action_count: int
    watch_count: int
    primary_work_item_id: str | None
    headline: str
    detail: str


@dataclass(frozen=True, slots=True)
class CockpitAttentionSummary:
    state: str
    action_count: int
    watch_count: int


@dataclass(frozen=True, slots=True)
class CockpitDevPoolSummary:
    capacity_limit: int | None
    capacity_used: int | None
    capacity_available: int | None
    candidates: int
    active: int
    waiting_for_capacity: int
    waiting_for_resource_lock: int


@dataclass(frozen=True, slots=True)
class CockpitOverviewProjection:
    project: Project
    observed_at: datetime
    roadmap_source: CockpitSource
    execution_source: CockpitSource
    attention_source: CockpitSource
    attention: CockpitAttentionSummary
    roles: tuple[CockpitRoleSummary, ...]
    now: CockpitHorizonItem | None
    parallel: tuple[CockpitHorizonItem, ...]
    next: CockpitHorizonItem | None
    dev_pool: CockpitDevPoolSummary


def _empty_attention() -> CockpitAttentionSummary:
    return CockpitAttentionSummary(state="UNAVAILABLE", action_count=0, watch_count=0)


def _empty_dev_pool() -> CockpitDevPoolSummary:
    return CockpitDevPoolSummary(
        capacity_limit=None,
        capacity_used=None,
        capacity_available=None,
        candidates=0,
        active=0,
        waiting_for_capacity=0,
        waiting_for_resource_lock=0,
    )


def _diagnostics_from_pipeline(pipeline) -> tuple[CockpitDiagnostic, ...]:
    return tuple(
        CockpitDiagnostic(item.code, item.message, item.line_number)
        for item in pipeline.diagnostics
    )


def _horizon_item(
    item: WorkItem,
    scheduler_by_key: dict[str, SchedulerItemProjection],
) -> CockpitHorizonItem:
    scheduler = scheduler_by_key.get(item.key)
    return CockpitHorizonItem(
        key=item.key,
        title=item.title,
        type=item.type.value,
        status=item.status.value,
        lane=item.lane,
        scheduler_state=scheduler.state.value if scheduler is not None else None,
        expected_role=scheduler.expected_role if scheduler is not None else None,
        next_action=scheduler.next_action.value if scheduler is not None else None,
    )


def _derive_horizons(pipeline, scheduler) -> tuple[
    CockpitHorizonItem | None,
    tuple[CockpitHorizonItem, ...],
    CockpitHorizonItem | None,
]:
    if not pipeline.valid or not scheduler.valid:
        return None, (), None

    scheduler_by_key = {item.work_item.key: item for item in scheduler.items}
    unfinished = {
        WorkItemStatus.READY,
        WorkItemStatus.BLOCKED,
    }
    main = [
        item
        for item in pipeline.work_items
        if item.lane == "MAIN" and item.status in unfinished
    ]
    parallel = [
        item
        for item in pipeline.work_items
        if item.lane != "MAIN" and item.status in unfinished
    ]

    now = _horizon_item(main[0], scheduler_by_key) if main else None
    next_item = _horizon_item(main[1], scheduler_by_key) if len(main) > 1 else None
    return (
        now,
        tuple(_horizon_item(item, scheduler_by_key) for item in parallel),
        next_item,
    )


def _role_attention_counts(
    projection: AttentionProjection | None,
    role: str,
) -> tuple[int, int, str | None]:
    if projection is None:
        return 0, 0, None
    matching = [item for item in projection.items if item.role == role]
    actions = sum(item.level.value == "ACTION" for item in matching)
    watches = sum(item.level.value == "WATCH" for item in matching)
    primary = matching[0].work_item_id if matching else None
    return actions, watches, primary


def _role_state(actions: int, watches: int) -> str:
    if actions:
        return "ACTION"
    if watches:
        return "WATCH"
    return "CLEAR"


def _role_summaries(
    attention: AttentionProjection | None,
    execution: ParallelDevExecutionProjection | None,
    *,
    now: CockpitHorizonItem | None,
    next_item: CockpitHorizonItem | None,
) -> tuple[CockpitRoleSummary, ...]:
    po_actions, po_watches, po_work_item = _role_attention_counts(attention, "PO")
    arch_actions, arch_watches, arch_work_item = _role_attention_counts(attention, "ARCH")

    po_detail = (
        f"Maintenant : {now.key} · ensuite : {next_item.key}."
        if now is not None and next_item is not None
        else f"Maintenant : {now.key}."
        if now is not None
        else "Aucun WorkItem MAIN actif dans la projection."
    )
    arch_detail = (
        f"Gate {arch_work_item} requiert une intervention."
        if arch_work_item is not None
        else "Aucune intervention Architecte signalée par l’Attention Center."
    )

    review_items = list(execution.items) if execution is not None else []
    open_prs = sum(
        item.execution.pull_request is not None and not item.execution.pull_request.merged
        for item in review_items
    )
    ci_red = sum(item.execution.state.value == "CI_RED" for item in review_items)
    ci_running = sum(item.execution.state.value == "CI_RUNNING" for item in review_items)
    reviewer_state = "ACTION" if ci_red else "WATCH" if open_prs or ci_running else "CLEAR"
    reviewer_headline = (
        f"{ci_red} CI rouge à examiner"
        if ci_red
        else f"{open_prs} PR ouverte(s) sous supervision"
        if open_prs
        else "Aucune revue urgente"
    )
    reviewer_detail = (
        f"CI en cours : {ci_running} · PR ouvertes : {open_prs}."
        if execution is not None
        else "Projection d’exécution indisponible."
    )

    return (
        CockpitRoleSummary(
            role="PO",
            label="Product Owner",
            state=_role_state(po_actions, po_watches),
            action_count=po_actions,
            watch_count=po_watches,
            primary_work_item_id=po_work_item or (now.key if now is not None else None),
            headline="Décision produit requise" if po_actions else "Roadmap sous supervision",
            detail=po_detail,
        ),
        CockpitRoleSummary(
            role="ARCH",
            label="Architecte",
            state=_role_state(arch_actions, arch_watches),
            action_count=arch_actions,
            watch_count=arch_watches,
            primary_work_item_id=arch_work_item,
            headline="Autorisation ou décision requise" if arch_actions else "Architecture sous supervision",
            detail=arch_detail,
        ),
        CockpitRoleSummary(
            role="REVIEWER",
            label="Reviewer",
            state=reviewer_state,
            action_count=ci_red,
            watch_count=max(0, open_prs + ci_running - ci_red),
            primary_work_item_id=next(
                (
                    item.execution.work_item.key
                    for item in review_items
                    if item.execution.work_item is not None and item.execution.pull_request is not None
                ),
                None,
            ),
            headline=reviewer_headline,
            detail=reviewer_detail,
        ),
    )


def _dev_pool_summary(
    projection: ParallelDevExecutionProjection | None,
) -> CockpitDevPoolSummary:
    if projection is None:
        return _empty_dev_pool()
    return CockpitDevPoolSummary(
        capacity_limit=projection.max_parallel_dev_executions,
        capacity_used=projection.active_count,
        capacity_available=projection.available_capacity,
        candidates=len(projection.items),
        active=sum(item.active for item in projection.items),
        waiting_for_capacity=sum(item.waiting_for_capacity for item in projection.items),
        waiting_for_resource_lock=sum(item.waiting_for_resource_lock for item in projection.items),
    )


def read_project_cockpit_overview(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
    max_parallel_dev_executions: int,
    companion_connected: bool,
    now: datetime | None = None,
) -> CockpitOverviewProjection:
    observed_at = now or datetime.now(timezone.utc)

    try:
        roadmap = read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError as exc:
        unavailable = CockpitSource(status="unavailable", code=exc.code)
        return CockpitOverviewProjection(
            project=project,
            observed_at=observed_at,
            roadmap_source=unavailable,
            execution_source=CockpitSource(status="unavailable", code="ROADMAP_UNAVAILABLE"),
            attention_source=CockpitSource(status="unavailable", code="ROADMAP_UNAVAILABLE"),
            attention=_empty_attention(),
            roles=_role_summaries(None, None, now=None, next_item=None),
            now=None,
            parallel=(),
            next=None,
            dev_pool=_empty_dev_pool(),
        )

    scheduler = derive_scheduler_projection(roadmap.pipeline)
    roadmap_source = CockpitSource(
        status="available",
        updated_at=roadmap.issue.updated_at,
        revision=sha256(roadmap.issue.body.encode("utf-8")).hexdigest(),
        diagnostics=_diagnostics_from_pipeline(roadmap.pipeline),
    )
    current, parallel, next_item = _derive_horizons(roadmap.pipeline, scheduler)

    try:
        execution = read_project_parallel_dev_executions(
            project,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=max_parallel_dev_executions,
            now=observed_at,
        )
    except RoadmapSourceError as exc:
        execution = None
        execution_source = CockpitSource(status="unavailable", code=exc.code)
    else:
        execution_diagnostics = tuple(
            CockpitDiagnostic(diagnostic.code, diagnostic.message)
            for item in execution.items
            for diagnostic in item.execution.diagnostics
        )
        execution_source = CockpitSource(
            status="available",
            updated_at=execution.issue.updated_at,
            diagnostics=execution_diagnostics,
        )

    try:
        attention = read_project_attention(
            project,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=max_parallel_dev_executions,
            companion_connected=companion_connected,
        )
    except RoadmapSourceError as exc:
        attention = None
        attention_source = CockpitSource(status="unavailable", code=exc.code)
        attention_summary = _empty_attention()
    else:
        attention_source = CockpitSource(status="available")
        attention_summary = CockpitAttentionSummary(
            state=attention.state.value,
            action_count=attention.action_count,
            watch_count=attention.watch_count,
        )

    return CockpitOverviewProjection(
        project=project,
        observed_at=observed_at,
        roadmap_source=roadmap_source,
        execution_source=execution_source,
        attention_source=attention_source,
        attention=attention_summary,
        roles=_role_summaries(attention, execution, now=current, next_item=next_item),
        now=current,
        parallel=parallel,
        next=next_item,
        dev_pool=_dev_pool_summary(execution),
    )
