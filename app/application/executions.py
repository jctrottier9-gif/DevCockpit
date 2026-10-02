from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    UnitOfWorkFactory,
    create_prompt_dispatch_in_uow,
)
from app.application.roadmaps import RoadmapIssueReader, RoadmapSourceError, read_project_roadmap
from app.domain.execution import (
    ExecutionDiagnostic,
    ExecutionEvidence,
    ExecutionProjection,
    ExecutionState,
    NextAction,
    blocked_projection,
    derive_execution_projection,
)
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole
from app.domain.roadmap import WorkItem, WorkItemType


class ExecutionSourceError(RuntimeError):
    code = "GITHUB_UNAVAILABLE"


class ExecutionAuthorizationError(ExecutionSourceError):
    code = "GITHUB_AUTHORIZATION_FAILED"


class ExecutionRepositoryNotFoundError(ExecutionSourceError):
    code = "GITHUB_REPOSITORY_NOT_FOUND"


class ExecutionPayloadError(ExecutionSourceError):
    code = "GITHUB_PAYLOAD_INVALID"


class ExecutionEvidenceReader(Protocol):
    def read(self, project: Project, work_item: WorkItem) -> ExecutionEvidence: ...


@dataclass(frozen=True, slots=True)
class ExecutionEvaluation:
    projection: ExecutionProjection
    dispatch: PromptDispatch | None


def read_project_execution(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
) -> ExecutionProjection:
    try:
        roadmap = read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError as exc:
        return blocked_projection(
            work_item=None,
            code=exc.code,
            message="Canonical roadmap source is unavailable.",
        )

    if not roadmap.pipeline.valid:
        return ExecutionProjection(
            work_item=None,
            state=ExecutionState.BLOCKED,
            next_action=NextAction.RESOLVE_BLOCKER,
            diagnostics=tuple(
                ExecutionDiagnostic(code=item.code, message=item.message)
                for item in roadmap.pipeline.diagnostics
            ),
        )

    work_item = roadmap.pipeline.active_ready_item
    if work_item is None:
        return blocked_projection(
            work_item=None,
            code="NO_MAIN_READY",
            message="The canonical MAIN lane does not currently contain a READY WorkItem.",
        )

    if work_item.type is not WorkItemType.WORK:
        return blocked_projection(
            work_item=work_item,
            code="ROLE_ROUTING_NOT_AVAILABLE",
            message=(
                f"{work_item.key} is an architecture gate; DC-021 does not route "
                "Architect or Product Owner work."
            ),
        )

    try:
        evidence = evidence_reader.read(project, work_item)
    except ExecutionSourceError as exc:
        return blocked_projection(
            work_item=work_item,
            code=exc.code,
            message="GitHub execution evidence is unavailable.",
        )

    return derive_execution_projection(work_item, evidence)


def evaluate_project_execution(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
) -> ExecutionEvaluation:
    projection = read_project_execution(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )

    dispatch: PromptDispatch | None = None
    work_item = projection.work_item
    if work_item is None:
        return ExecutionEvaluation(projection=projection, dispatch=None)

    with uow_factory() as uow:
        if uow.handoffs.active(project.project_id, work_item.key):
            return ExecutionEvaluation(projection=projection, dispatch=None)
        if (projection.next_action is NextAction.FIX_CI and
                uow.handoffs.covers(project.project_id, work_item.key,
                                   _ci_red_idempotency_key(project, projection))):
            return ExecutionEvaluation(projection=projection, dispatch=None)
        if projection.next_action is NextAction.START_DEV:
            dispatch = create_prompt_dispatch_in_uow(
                CreatePromptDispatchCommand(
                    project_id=project.project_id,
                    work_item_id=work_item.key,
                    role=PromptDispatchRole.DEV,
                    prompt_text=build_initial_dev_prompt(project, work_item),
                    idempotency_key=_initial_idempotency_key(project, work_item),
                ),
                uow=uow,
            )
        elif projection.next_action is NextAction.FIX_CI:
            dispatch = create_prompt_dispatch_in_uow(
                CreatePromptDispatchCommand(
                    project_id=project.project_id,
                    work_item_id=work_item.key,
                    role=PromptDispatchRole.DEV,
                    prompt_text=build_ci_red_follow_up(project, projection),
                    idempotency_key=_ci_red_idempotency_key(project, projection),
                ),
                uow=uow,
            )
        uow.commit()

    return ExecutionEvaluation(projection=projection, dispatch=dispatch)


def build_initial_dev_prompt(project: Project, work_item: WorkItem) -> str:
    return f"""Tu travailles sur le dépôt GitHub `{project.repository_full_name}`.

Prends en charge la tranche {work_item.key} — {work_item.title}.

Contexte canonique :
- Project : {project.project_id}
- WorkItem : {work_item.key}
- Parent : {work_item.parent}
- Roadmap maître : #{project.roadmap_issue_number}
- Statut canonique : READY

Travaille sur le main actuel et synchronise-toi avec le vrai main avant de commencer. Lis d'abord AGENTS.md, consulte le roadmap maître #{project.roadmap_issue_number} et son bloc canonique COCKPIT_PIPELINE_V1 ou COCKPIT_PIPELINE_V2 explicitement présent, puis consulte le WorkItem {work_item.key} et les ADR applicables. Inspecte le code et les tests existants avant de modifier quoi que ce soit.

Implémente uniquement la tranche autorisée, respecte strictement son scope et poursuis jusqu'au cycle de livraison prévu dans AGENTS.md. Ne commence pas la tranche suivante.
"""


def build_ci_red_follow_up(
    project: Project,
    projection: ExecutionProjection,
) -> str:
    work_item = projection.work_item
    pull_request = projection.pull_request
    ci = projection.ci
    if work_item is None or pull_request is None or ci is None:
        raise ValueError("CI_RED follow-up requires WorkItem, pull request and CI evidence")

    failed_jobs = "\n".join(f"- {job}" for job in ci.failed_jobs) or "- échec de workflow (job précis non disponible)"
    run_lines = "\n".join(
        (
            f"- {run.name}: run {run.run_id}, attempt {run.attempt}, "
            f"status={run.status}, conclusion={run.conclusion or 'none'}"
        )
        for run in ci.runs
        if run.status == "completed"
        and run.conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    )
    github_url = pull_request.url or "non disponible"

    return f"""La CI de la PR #{pull_request.number} pour {work_item.key} vient d'échouer sur le head SHA {pull_request.head_sha}.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR : #{pull_request.number}
GitHub : {github_url}

Validations rouges observées :
{run_lines or "- validation rouge détectée"}

Jobs/checks rouges connus :
{failed_jobs}

Analyse les échecs actuels sur GitHub, corrige uniquement ce qui relève de {work_item.key}, exécute les validations pertinentes, pousse les corrections et poursuis jusqu'au cycle prévu dans AGENTS.md.

Reste strictement dans le scope du WorkItem {work_item.key}. Ne commence pas la tranche suivante.
"""


def _initial_idempotency_key(project: Project, work_item: WorkItem) -> str:
    return _bounded_idempotency_key(
        f"execution:{project.project_id}:{work_item.key}:DEV:INITIAL:v1"
    )


def _ci_red_idempotency_key(
    project: Project,
    projection: ExecutionProjection,
) -> str:
    work_item = projection.work_item
    pull_request = projection.pull_request
    ci = projection.ci
    if (
        work_item is None
        or pull_request is None
        or ci is None
        or ci.failure_run_id is None
        or ci.failure_run_attempt is None
    ):
        raise ValueError("CI_RED idempotency requires immutable GitHub failure evidence")
    return _bounded_idempotency_key(
        "execution:"
        f"{project.project_id}:{work_item.key}:DEV:CI_RED:"
        f"pr{pull_request.number}:{pull_request.head_sha}:"
        f"run{ci.failure_run_id}:attempt{ci.failure_run_attempt}:v1"
    )


def _bounded_idempotency_key(raw: str) -> str:
    if len(raw) <= 200:
        return raw
    return f"execution:{sha256(raw.encode('utf-8')).hexdigest()}:v1"
