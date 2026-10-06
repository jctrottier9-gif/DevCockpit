from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Protocol

from app.application.architecture_gates import (
    architecture_gate_authorization_idempotency_key,
)
from app.application.github_wait_watchdogs import (
    GitHubWaitWatchdog,
    GitHubWaitWatchdogKind,
    project_execution_github_wait_watchdog,
    watchdog_dispatch_key,
    with_recovery,
)
from app.application.pr_finalization import finalization_idempotency_key
from app.application.prompt_dispatches import UnitOfWorkFactory
from app.application.roadmap_explorer import (
    RoadmapExplorerIssueReader,
    read_project_roadmap_explorer,
)
from app.application.roadmaps import RoadmapIssueReader, read_project_roadmap
from app.domain.execution import (
    CiState,
    CiSummary,
    ExecutionProjection,
    ExecutionState,
    NextAction,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.pr_finalization import FinalizationAttemptStatus, FinalizationOperation
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
    created_at: str | None = None
    updated_at: str | None = None


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
    base_branch: str | None
    base_sha: str | None
    behind_by: int | None
    finalization_state: str | None
    finalization_detail: str | None
    ci_state: str
    workflows: tuple[ReviewWorkflowEvidence, ...]
    created_at: str | None = None
    updated_at: str | None = None
    github_watchdog: GitHubWaitWatchdog | None = None
    mergeable_state: str | None = None


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


def _review_execution_projection(
    pull_request: ReviewPullRequestEvidence,
    *,
    work_item: WorkItem,
) -> ExecutionProjection | None:
    state_by_ci = {
        "NOT_OBSERVED": (ExecutionState.PR_OPEN, NextAction.WAIT),
        "RUNNING": (ExecutionState.CI_RUNNING, NextAction.WAIT),
    }
    projected = state_by_ci.get(pull_request.ci_state)
    if projected is None:
        if (
            pull_request.ci_state == "GREEN"
            and pull_request.auto_merge_enabled
            and (pull_request.behind_by or 0) == 0
        ):
            projected = (ExecutionState.READY_TO_MERGE, NextAction.WAIT_AUTO_MERGE)
        else:
            return None

    try:
        ci_state = CiState(pull_request.ci_state)
    except ValueError:
        return None

    domain_pr = PullRequestEvidence(
        number=pull_request.number,
        title=pull_request.title,
        body="",
        branch=pull_request.branch,
        head_sha=pull_request.head_sha,
        state="open",
        merged=False,
        mergeable=pull_request.mergeable,
        base_branch=pull_request.base_branch,
        base_sha=pull_request.base_sha,
        behind_by=pull_request.behind_by,
        auto_merge_enabled=pull_request.auto_merge_enabled,
        url=pull_request.url,
        created_at=pull_request.created_at,
        updated_at=pull_request.updated_at,
    )
    domain_runs = tuple(
        WorkflowRunEvidence(
            run_id=run.run_id,
            name=run.name,
            status=run.status,
            conclusion=run.conclusion,
            attempt=run.attempt,
            head_sha=run.head_sha,
            url=run.url,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
        for run in pull_request.workflows
    )
    return ExecutionProjection(
        work_item=work_item,
        state=projected[0],
        next_action=projected[1],
        pull_request=domain_pr,
        ci=CiSummary(
            state=ci_state,
            observed_runs=len(domain_runs),
            failed_jobs=(),
            runs=domain_runs,
        ),
    )


def _review_watchdog(
    project: Project,
    pull_request: ReviewPullRequestEvidence,
    *,
    work_item: WorkItem,
    now: datetime,
    pr_no_ci_after_seconds: float,
    ci_stall_after_seconds: float,
    auto_merge_grace_seconds: float,
) -> tuple[ExecutionProjection | None, GitHubWaitWatchdog | None]:
    execution = _review_execution_projection(pull_request, work_item=work_item)
    if execution is None:
        return None, None
    return execution, project_execution_github_wait_watchdog(
        execution,
        now=now,
        pr_no_ci_after_seconds=pr_no_ci_after_seconds,
        ci_stall_after_seconds=ci_stall_after_seconds,
        auto_merge_grace_seconds=auto_merge_grace_seconds,
    )


def read_project_review_panel(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    review_reader: ReviewEvidenceReader,
    uow_factory: UnitOfWorkFactory | None = None,
    pr_no_ci_after_seconds: float = 900.0,
    ci_stall_after_seconds: float = 1800.0,
    auto_merge_grace_seconds: float = 600.0,
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
    pull_requests = evidence.pull_requests
    current = now or datetime.now(timezone.utc)
    work_by_key = {item.key: item for item in active_work}
    if uow_factory is not None:
        with uow_factory() as uow:
            attempts = getattr(uow, "pr_finalization_attempts", None)
            projected = []
            for pull_request in pull_requests:
                attempt = None
                if attempts is not None:
                    matching = [
                        candidate
                        for candidate in attempts.list_for_pr(
                            project.project_id,
                            pull_request.work_item_id,
                            pull_request.number,
                        )
                        if candidate.expected_head_sha == pull_request.head_sha
                        and (
                            candidate.operation is FinalizationOperation.MERGE_PR
                            or candidate.base_sha == pull_request.base_sha
                        )
                    ]
                    attempt = matching[0] if matching else None

                state = pull_request.finalization_state
                detail = pull_request.finalization_detail
                if attempt is not None:
                    detail = attempt.message or attempt.error_code
                    if attempt.status is FinalizationAttemptStatus.BLOCKED:
                        state = (
                            "BRANCH_SYNC_BLOCKED"
                            if attempt.operation is FinalizationOperation.SYNC_BRANCH
                            else "MERGE_BLOCKED"
                        )
                    elif attempt.status is FinalizationAttemptStatus.IN_PROGRESS:
                        state = "FINALIZATION_IN_PROGRESS"
                    elif attempt.status is FinalizationAttemptStatus.SUCCEEDED:
                        state = (
                            "SYNC_BRANCH_ACCEPTED"
                            if attempt.operation is FinalizationOperation.SYNC_BRANCH
                            else "MERGE_ACCEPTED"
                        )
                    elif attempt.status is FinalizationAttemptStatus.STALE:
                        state = "FINALIZATION_STALE"

                updated_pull_request = replace(
                    pull_request,
                    finalization_state=state,
                    finalization_detail=detail,
                )
                work_item = work_by_key.get(updated_pull_request.work_item_id)
                watchdog = None
                if work_item is not None:
                    watchdog_execution, watchdog = _review_watchdog(
                        project,
                        updated_pull_request,
                        work_item=work_item,
                        now=current,
                        pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                        ci_stall_after_seconds=ci_stall_after_seconds,
                        auto_merge_grace_seconds=auto_merge_grace_seconds,
                    )
                    if watchdog is not None:
                        if watchdog.kind in {
                            GitHubWaitWatchdogKind.PR_NO_CI,
                            GitHubWaitWatchdogKind.CI_STALLED,
                        }:
                            prepared = (
                                uow.prompt_dispatches.get_by_idempotency_key(
                                    watchdog_dispatch_key(
                                        project,
                                        updated_pull_request.work_item_id,
                                        watchdog,
                                    )
                                )
                                is not None
                            )
                            watchdog = with_recovery(
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
                        elif watchdog_execution is not None and attempts is not None:
                            recovery_projection = replace(
                                watchdog_execution,
                                next_action=NextAction.MERGE_PR,
                            )
                            try:
                                recovery_key = finalization_idempotency_key(
                                    project,
                                    recovery_projection,
                                )
                            except ValueError:
                                recovery_attempt = None
                            else:
                                recovery_attempt = attempts.get_by_idempotency_key(
                                    recovery_key
                                )
                            watchdog = with_recovery(
                                watchdog,
                                prepared=recovery_attempt is not None,
                                state=(
                                    f"FINALIZER_{recovery_attempt.status.value}"
                                    if recovery_attempt is not None
                                    else "FINALIZER_DUE"
                                    if watchdog.due
                                    else "WAITING_FOR_GITHUB"
                                ),
                            )
                projected.append(
                    replace(updated_pull_request, github_watchdog=watchdog)
                )
            pull_requests = tuple(projected)
    elif pull_requests:
        pull_requests = tuple(
            replace(
                pull_request,
                github_watchdog=(
                    _review_watchdog(
                        project,
                        pull_request,
                        work_item=work_by_key[pull_request.work_item_id],
                        now=current,
                        pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                        ci_stall_after_seconds=ci_stall_after_seconds,
                        auto_merge_grace_seconds=auto_merge_grace_seconds,
                    )[1]
                    if pull_request.work_item_id in work_by_key
                    else None
                ),
            )
            for pull_request in pull_requests
        )
    return ReviewPanelProjection(
        project=project,
        observed_at=now or datetime.now(timezone.utc),
        roadmap_updated_at=roadmap.issue.updated_at,
        revision=sha256(roadmap.issue.body.encode("utf-8")).hexdigest(),
        pull_requests=pull_requests,
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
