from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Protocol

from app.application.architecture_gates import (
    architecture_gate_authorization_idempotency_key,
)
from app.application.prompt_dispatches import UnitOfWorkFactory
from app.application.roadmap_explorer import (
    RoadmapExplorerIssueReader,
    read_project_roadmap_explorer,
)
from app.application.roadmaps import RoadmapIssueReader, read_project_roadmap
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatchRole
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType


_ADR_ID = re.compile(r"\bADR-[0-9]{4}\b")
_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_RUNNING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


@dataclass(frozen=True, slots=True)
class PanelDiagnostic:
    code: str
    message: str
    work_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArchitectureDocumentReference:
    adr_id: str
    title: str
    path: str
    url: str


@dataclass(frozen=True, slots=True)
class ArchitectureDocumentDetail:
    reference: ArchitectureDocumentReference
    content: str


class ArchitectureDocumentError(RuntimeError):
    code = "ARCHITECTURE_DOCUMENT_UNAVAILABLE"


class ArchitectureDocumentNotFound(ArchitectureDocumentError):
    code = "ARCHITECTURE_DOCUMENT_NOT_FOUND"


class ArchitectureDocumentReader(Protocol):
    def list(self, repository_full_name: str) -> tuple[ArchitectureDocumentReference, ...]: ...

    def read(
        self,
        repository_full_name: str,
        path: str,
    ) -> ArchitectureDocumentDetail: ...


@dataclass(frozen=True, slots=True)
class ArchitectureAuthorizationEvidence:
    dispatch_id: str
    agent_session: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ArchitectureGatePanelItem:
    work_item_id: str
    title: str
    status: str
    lane: str
    parent: str
    scheduler_state: str | None
    scheduler_reason: str | None
    executable: bool
    human_authorization_required: bool
    can_authorize: bool
    authorization: ArchitectureAuthorizationEvidence | None
    work_issue_number: int | None
    work_issue_url: str | None
    parent_issue_number: int | None
    parent_issue_url: str | None
    adrs: tuple[ArchitectureDocumentReference, ...]


@dataclass(frozen=True, slots=True)
class ArchitecturePanelProjection:
    project: Project
    observed_at: datetime
    roadmap_updated_at: str | None
    revision: str
    gates: tuple[ArchitectureGatePanelItem, ...]
    diagnostics: tuple[PanelDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ReviewJobEvidence:
    job_id: int
    name: str
    status: str
    conclusion: str | None
    url: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ReviewWorkflowEvidence:
    run_id: int
    name: str
    status: str
    conclusion: str | None
    attempt: int
    head_sha: str
    url: str | None
    jobs: tuple[ReviewJobEvidence, ...]
    jobs_complete: bool


@dataclass(frozen=True, slots=True)
class ReviewPullRequestEvidence:
    work_item_id: str
    work_item_title: str
    lane: str
    number: int
    title: str
    branch: str
    head_sha: str
    url: str | None
    mergeable: bool | None
    auto_merge_enabled: bool
    ci_state: str
    workflows: tuple[ReviewWorkflowEvidence, ...]


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    pull_requests: tuple[ReviewPullRequestEvidence, ...]
    complete: bool = True
    diagnostics: tuple[PanelDiagnostic, ...] = ()


class ReviewEvidenceReader(Protocol):
    def read(
        self,
        project: Project,
        work_items: tuple[WorkItem, ...],
    ) -> ReviewEvidence: ...


@dataclass(frozen=True, slots=True)
class ReviewPanelProjection:
    project: Project
    observed_at: datetime
    roadmap_updated_at: str | None
    revision: str
    pull_requests: tuple[ReviewPullRequestEvidence, ...]
    complete: bool
    diagnostics: tuple[PanelDiagnostic, ...]


class ArchitectureAdrNotReferencedError(ValueError):
    code = "ADR_NOT_REFERENCED"


def _authorization_evidence(project: Project, work_item_id: str, *, uow_factory: UnitOfWorkFactory):
    idempotency_key = architecture_gate_authorization_idempotency_key(
        project,
        work_item_id,
    )
    with uow_factory() as uow:
        dispatch = uow.prompt_dispatches.get_by_idempotency_key(idempotency_key)
    if dispatch is None:
        return None
    if (
        dispatch.project_id != project.project_id
        or dispatch.work_item_id != work_item_id
        or dispatch.role is not PromptDispatchRole.ARCH
    ):
        return None
    return ArchitectureAuthorizationEvidence(
        dispatch_id=str(dispatch.dispatch_id),
        agent_session=dispatch.agent_session,
        status=dispatch.status.value,
        created_at=dispatch.created_at.isoformat(),
    )


def _explicit_adr_ids(*bodies: str) -> tuple[str, ...]:
    found: set[str] = set()
    ordered: list[str] = []
    for body in bodies:
        for match in _ADR_ID.finditer(body):
            adr_id = match.group(0)
            if adr_id not in found:
                found.add(adr_id)
                ordered.append(adr_id)
    return tuple(ordered)


def read_project_architecture_panel(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    issue_reader: RoadmapExplorerIssueReader,
    document_reader: ArchitectureDocumentReader,
    uow_factory: UnitOfWorkFactory,
    now: datetime | None = None,
) -> ArchitecturePanelProjection:
    explorer = read_project_roadmap_explorer(project, roadmap_reader=roadmap_reader)
    diagnostics: list[PanelDiagnostic] = []

    try:
        catalog = document_reader.list(project.repository_full_name)
    except ArchitectureDocumentError as exc:
        catalog = ()
        diagnostics.append(
            PanelDiagnostic(exc.code, "Architecture documents are unavailable.")
        )
    catalog_by_id = {item.adr_id: item for item in catalog}

    issue_bodies: dict[int, str] = {}
    gate_rows = [item for item in explorer.items if item.type == WorkItemType.ARCHITECTURE_GATE.value]
    referenced_issue_numbers = {
        reference.number
        for item in gate_rows
        for reference in (item.work_issue, item.parent_issue)
        if reference is not None
    }
    for issue_number in sorted(referenced_issue_numbers):
        try:
            issue_bodies[issue_number] = issue_reader.read(
                project.repository_full_name,
                issue_number,
            ).body
        except Exception as exc:
            code = getattr(exc, "code", "GITHUB_ISSUE_UNAVAILABLE")
            diagnostics.append(
                PanelDiagnostic(
                    code,
                    f"Unable to read explicitly referenced GitHub issue #{issue_number}.",
                )
            )

    gates: list[ArchitectureGatePanelItem] = []
    for item in gate_rows:
        authorization = _authorization_evidence(
            project,
            item.key,
            uow_factory=uow_factory,
        )
        bodies = tuple(
            issue_bodies[reference.number]
            for reference in (item.work_issue, item.parent_issue)
            if reference is not None and reference.number in issue_bodies
        )
        explicit_ids = _explicit_adr_ids(*bodies)
        adrs: list[ArchitectureDocumentReference] = []
        for adr_id in explicit_ids:
            reference = catalog_by_id.get(adr_id)
            if reference is None:
                diagnostics.append(
                    PanelDiagnostic(
                        "ADR_EXPLICIT_REFERENCE_NOT_FOUND",
                        f"{adr_id} is explicitly referenced but no matching repository ADR was found.",
                        item.key,
                    )
                )
                continue
            adrs.append(reference)

        human_authorization_required = (
            item.status == WorkItemStatus.READY.value and authorization is None
        )
        can_authorize = (
            human_authorization_required
            and item.executable
            and item.expected_role == "ARCH"
            and item.next_action == "START_ARCH"
        )
        gates.append(
            ArchitectureGatePanelItem(
                work_item_id=item.key,
                title=item.title,
                status=item.status,
                lane=item.lane,
                parent=item.parent,
                scheduler_state=item.scheduler_state,
                scheduler_reason=item.scheduler_reason,
                executable=item.executable,
                human_authorization_required=human_authorization_required,
                can_authorize=can_authorize,
                authorization=authorization,
                work_issue_number=item.work_issue.number if item.work_issue is not None else None,
                work_issue_url=item.work_issue.url if item.work_issue is not None else None,
                parent_issue_number=item.parent_issue.number if item.parent_issue is not None else None,
                parent_issue_url=item.parent_issue.url if item.parent_issue is not None else None,
                adrs=tuple(adrs),
            )
        )

    return ArchitecturePanelProjection(
        project=project,
        observed_at=now or datetime.now(timezone.utc),
        roadmap_updated_at=explorer.roadmap_updated_at,
        revision=explorer.revision,
        gates=tuple(gates),
        diagnostics=tuple(diagnostics),
    )


def read_architecture_adr_detail(
    project: Project,
    work_item_id: str,
    adr_id: str,
    *,
    roadmap_reader: RoadmapIssueReader,
    issue_reader: RoadmapExplorerIssueReader,
    document_reader: ArchitectureDocumentReader,
    uow_factory: UnitOfWorkFactory,
) -> ArchitectureDocumentDetail:
    panel = read_project_architecture_panel(
        project,
        roadmap_reader=roadmap_reader,
        issue_reader=issue_reader,
        document_reader=document_reader,
        uow_factory=uow_factory,
    )
    gate = next((item for item in panel.gates if item.work_item_id == work_item_id), None)
    if gate is None:
        raise ArchitectureAdrNotReferencedError(work_item_id)
    reference = next((item for item in gate.adrs if item.adr_id == adr_id), None)
    if reference is None:
        raise ArchitectureAdrNotReferencedError(adr_id)
    return document_reader.read(project.repository_full_name, reference.path)


def read_project_review_panel(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    review_reader: ReviewEvidenceReader,
    now: datetime | None = None,
) -> ReviewPanelProjection:
    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    if not roadmap.pipeline.valid:
        diagnostics = tuple(
            PanelDiagnostic(item.code, item.message)
            for item in roadmap.pipeline.diagnostics
        )
        return ReviewPanelProjection(
            project=project,
            observed_at=now or datetime.now(timezone.utc),
            roadmap_updated_at=roadmap.issue.updated_at,
            revision=sha256(roadmap.issue.body.encode("utf-8")).hexdigest(),
            pull_requests=(),
            complete=True,
            diagnostics=diagnostics,
        )

    active_work = tuple(
        item
        for item in roadmap.pipeline.work_items
        if item.type is WorkItemType.WORK and item.status is WorkItemStatus.READY
    )
    evidence = review_reader.read(project, active_work)
    return ReviewPanelProjection(
        project=project,
        observed_at=now or datetime.now(timezone.utc),
        roadmap_updated_at=roadmap.issue.updated_at,
        revision=sha256(roadmap.issue.body.encode("utf-8")).hexdigest(),
        pull_requests=evidence.pull_requests,
        complete=evidence.complete,
        diagnostics=evidence.diagnostics,
    )


def review_ci_state(workflows: tuple[ReviewWorkflowEvidence, ...]) -> str:
    if not workflows:
        return "NOT_OBSERVED"
    if any(
        item.status == "completed" and item.conclusion in _FAILURE_CONCLUSIONS
        for item in workflows
    ):
        return "RED"
    if any(item.status in _RUNNING_STATUSES for item in workflows):
        return "RUNNING"
    if all(
        item.status == "completed" and item.conclusion in _GREEN_CONCLUSIONS
        for item in workflows
    ):
        return "GREEN"
    return "UNKNOWN"
