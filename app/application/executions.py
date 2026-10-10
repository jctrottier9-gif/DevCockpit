from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from app.application.delivery_contexts import dev_target_instructions, release_automation_allowed
from app.domain.delivery_context import DeliveryMode
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
from app.domain.scheduler import derive_scheduler_projection


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


def _execution_from_roadmap(
    project: Project,
    roadmap,
    work_item_id: str,
    *,
    evidence_reader: ExecutionEvidenceReader,
) -> ExecutionProjection:
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

    scheduler = derive_scheduler_projection(roadmap.pipeline)
    scheduler_item = next(
        (item for item in scheduler.items if item.work_item.key == work_item_id),
        None,
    )
    if scheduler_item is None:
        return blocked_projection(
            work_item=None,
            code="WORK_ITEM_NOT_FOUND",
            message=f"{work_item_id} is not present in the canonical scheduler projection.",
        )

    work_item = scheduler_item.work_item
    if not scheduler_item.executable:
        dependencies = ", ".join(scheduler_item.unsatisfied_dependencies)
        message = (
            f"{work_item.key} is not authorized by the deterministic scheduler."
            if not dependencies
            else f"{work_item.key} is waiting for dependencies: {dependencies}."
        )
        return blocked_projection(
            work_item=work_item,
            code=scheduler_item.reason.value,
            message=message,
        )

    if (
        work_item.type is not WorkItemType.WORK
        or scheduler_item.expected_role != PromptDispatchRole.DEV.value
    ):
        return blocked_projection(
            work_item=work_item,
            code="ROLE_ROUTING_NOT_AVAILABLE",
            message=(
                f"{work_item.key} is not an executable DEV WorkItem for this projection."
            ),
        )

    context = project.delivery_context_for(work_item.key)
    if not release_automation_allowed(context):
        return blocked_projection(
            work_item=work_item,
            code="RELEASE_AUTOMATION_DISABLED",
            message="This accepted delivery mode requires an explicit operation.",
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


def read_project_execution_for_work_item(
    project: Project,
    work_item_id: str,
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
    return _execution_from_roadmap(
        project,
        roadmap,
        work_item_id,
        evidence_reader=evidence_reader,
    )


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
        return _execution_from_roadmap(
            project,
            roadmap,
            "",
            evidence_reader=evidence_reader,
        )

    work_item = roadmap.pipeline.active_ready_item
    if work_item is None:
        return blocked_projection(
            work_item=None,
            code="NO_MAIN_READY",
            message="The canonical MAIN lane does not currently contain a READY WorkItem.",
        )

    return _execution_from_roadmap(
        project,
        roadmap,
        work_item.key,
        evidence_reader=evidence_reader,
    )

def evaluate_project_execution(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    evidence_reader: ExecutionEvidenceReader,
    uow_factory: UnitOfWorkFactory,
) -> ExecutionEvaluation:
    # Snapshot the target generation before reading remote evidence. A roadmap
    # application increments this generation when it claims the target. The
    # second check below is atomic with PromptDispatch creation under the same
    # SQLite BEGIN IMMEDIATE writer boundary.
    with uow_factory() as uow:
        initial_fence = uow.roadmap_target_fences.snapshot(
            project.repository_full_name,
            project.roadmap_issue_number,
        )

    projection = read_project_execution(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )

    dispatch: PromptDispatch | None = None
    work_item = projection.work_item
    if work_item is None:
        return ExecutionEvaluation(projection=projection, dispatch=None)
    if initial_fence.active_application_id is not None:
        return ExecutionEvaluation(projection=projection, dispatch=None)

    context = project.delivery_context_for(work_item.key)
    with uow_factory() as uow:
        if context is not None and context.mode in {DeliveryMode.HOTFIX, DeliveryMode.FORWARD_PORT}:
            try:
                accepted = uow.delivery_contexts.get(context.repository_id, work_item.key)
            except Exception:
                accepted = None
            if accepted != context:
                return ExecutionEvaluation(
                    projection=blocked_projection(
                        work_item=work_item, code="DELIVERY_NOT_ACCEPTED",
                        message="A matching persisted human-accepted delivery contract is required.",
                    ),
                    dispatch=None,
                )
        current_fence = uow.roadmap_target_fences.snapshot(
            project.repository_full_name,
            project.roadmap_issue_number,
        )
        if (
            current_fence.active_application_id is not None
            or current_fence.generation != initial_fence.generation
        ):
            return ExecutionEvaluation(projection=projection, dispatch=None)
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
        elif projection.next_action is NextAction.RECONCILE_ROADMAP:
            dispatch = create_prompt_dispatch_in_uow(
                CreatePromptDispatchCommand(
                    project_id=project.project_id,
                    work_item_id=work_item.key,
                    role=PromptDispatchRole.DEV,
                    prompt_text=build_roadmap_reconciliation_follow_up(project, projection),
                    idempotency_key=_roadmap_reconcile_idempotency_key(project, projection),
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

Vérifie et synchronise-toi avec la base Git explicitement autorisée avant de commencer. Lis d'abord AGENTS.md, consulte le roadmap maître #{project.roadmap_issue_number} et son bloc canonique COCKPIT_PIPELINE_V1, COCKPIT_PIPELINE_V2 ou COCKPIT_PIPELINE_V3 explicitement présent, puis consulte le WorkItem {work_item.key} et les ADR applicables. Inspecte le code et les tests existants avant de modifier quoi que ce soit.

Implémente uniquement la tranche autorisée et respecte strictement son scope.

Quand la PR est complète, active l'auto-merge si les règles du dépôt le permettent, rapporte le numéro de PR et le head SHA courant, puis ARRÊTE ton tour DEV. Ne reste pas à poller ou attendre la CI : DevCockpit observe GitHub et te renverra un prompt dans cette même session uniquement si une intervention DEV est nécessaire. Ne commence pas la tranche suivante.
""" + dev_target_instructions(project.delivery_context_for(work_item.key))


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

Reste strictement dans le scope du WorkItem {work_item.key}. Après avoir poussé la correction, vérifie que l'auto-merge demeure armé lorsque permis, rapporte le nouveau head SHA, puis ARRÊTE ton tour DEV. Ne reste pas à poller la CI : DevCockpit reprend l'observation GitHub. Ne commence pas la tranche suivante.
""" + dev_target_instructions(project.delivery_context_for(work_item.key))


def build_stale_dev_follow_up(
    project: Project,
    projection: ExecutionProjection,
    *,
    inactivity_seconds: float,
) -> str:
    work_item = projection.work_item
    branch = projection.branch
    if (
        work_item is None
        or branch is None
        or projection.state is not ExecutionState.DEVELOPING
        or branch.last_activity_at is None
    ):
        raise ValueError(
            "stale DEV follow-up requires DEVELOPING WorkItem and branch activity evidence"
        )

    inactivity_minutes = max(1, round(inactivity_seconds / 60))
    return f"""La session DEV de {work_item.key} semble interrompue ou inactive.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
Branche observée : {branch.name}
Head SHA observé : {branch.sha}
Dernière activité GitHub sur la branche : {branch.last_activity_at}
Inactivité observée : au moins {inactivity_minutes} minutes
Roadmap maître : #{project.roadmap_issue_number}

Reprends le travail existant dans la même session DEV à partir de l'état GitHub actuel.

- vérifie la base Git attendue pour ce WorkItem;
- relis AGENTS.md, le roadmap canonique et le WorkItem {work_item.key};
- inspecte la branche existante {branch.name} et ses commits avant toute modification;
- ne recommence pas la tranche depuis zéro et ne duplique pas le travail déjà poussé;
- détermine ce qui reste réellement à terminer pour {work_item.key};
- poursuis uniquement dans le scope de {work_item.key};
- si une PR existe désormais, reprends-la plutôt que d'en créer une concurrente;
- quand la PR est complète, assure-toi que l'auto-merge est armé lorsque permis, rapporte la PR et le head SHA, puis ARRÊTE ton tour DEV;
- ne reste pas à poller la CI et ne commence pas la tranche suivante.

Cette relance est un watchdog d'inactivité : GitHub demeure la source de vérité.
""" + dev_target_instructions(project.delivery_context_for(work_item.key))


def _stale_dev_idempotency_key(
    project: Project,
    projection: ExecutionProjection,
) -> str:
    work_item = projection.work_item
    branch = projection.branch
    if (
        work_item is None
        or branch is None
        or projection.state is not ExecutionState.DEVELOPING
        or branch.last_activity_at is None
    ):
        raise ValueError(
            "stale DEV idempotency requires immutable branch activity evidence"
        )
    return _bounded_idempotency_key(
        "execution:"
        f"{project.project_id}:{work_item.key}:DEV:STALE:"
        f"{branch.name}:{branch.sha}:{branch.last_activity_at}:v1"
    )


def build_roadmap_reconciliation_follow_up(
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
        or projection.state is not ExecutionState.ROADMAP_UPDATE_REQUIRED
    ):
        raise ValueError(
            "ROADMAP_UPDATE_REQUIRED follow-up requires WorkItem, merged PR and green CI evidence"
        )

    github_url = pull_request.url or "non disponible"
    merged_at = pull_request.merged_at or "non disponible"

    return f"""La livraison GitHub de {work_item.key} est fusionnée et les validations requises sont vertes. Le roadmap canonique doit maintenant être réconcilié.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR : #{pull_request.number}
GitHub : {github_url}
Head SHA livré : {pull_request.head_sha}
Merged at : {merged_at}
Roadmap maître : #{project.roadmap_issue_number}
État observé : ROADMAP_UPDATE_REQUIRED

Reprends la même session DEV pour effectuer uniquement la réconciliation post-merge du roadmap.

1. Vérifie la base Git exacte acceptée pour cette livraison.
2. Relis AGENTS.md et le roadmap maître #{project.roadmap_issue_number}, y compris son bloc canonique présent.
3. Vérifie sur GitHub que la PR #{pull_request.number} est réellement fusionnée et que les validations requises du head livré sont vertes.
4. Relis immédiatement la version courante du roadmap avant de l'éditer afin de ne pas écraser une modification concurrente.
5. Mets directement à jour le roadmap GitHub, sans confirmation humaine supplémentaire, pour refléter la livraison réelle :
   - marque {work_item.key} DONE;
   - promeus uniquement le vrai prochain WorkItem autorisé à READY selon l'ordre, les dépendances et les gates déjà définis;
   - garde les étapes ultérieures BLOCKED lorsqu'elles ne sont pas encore autorisées;
   - garde le texte humain/checklists cohérent avec le bloc canonique lorsque le contrat du dépôt l'exige.
6. Ne change pas le scope, l'ordre, les dépendances, REPLACES, le découpage ou l'identité des WorkItems pendant cette réconciliation déterministe. Si une telle modification structurelle est nécessaire, n'improvise pas : rapporte le blocage.
7. Si le prochain WorkItem est une ARCHITECTURE_GATE, tu peux uniquement le rendre READY dans le roadmap. Ne lance pas l'analyse ARCH et ne crée pas son prompt : DevCockpit exige une autorisation humaine distincte pour démarrer une gate architecturale.
8. Après l'édition, relis le roadmap GitHub et vérifie que le bloc canonique expose exactement l'état attendu.

Cette réconciliation post-merge remplace le comportement historique où le DEV mettait lui-même le roadmap à jour après le merge. Elle est autorisée sans preview/confirmation humaine supplémentaire parce qu'elle ne fait que réconcilier une livraison déjà prouvée par GitHub.

Ne commence pas le WorkItem suivant. Rapporte l'état final du roadmap puis ARRÊTE ton tour DEV.
""" + dev_target_instructions(project.delivery_context_for(work_item.key))


def _roadmap_reconcile_idempotency_key(
    project: Project,
    projection: ExecutionProjection,
) -> str:
    work_item = projection.work_item
    pull_request = projection.pull_request
    if (
        work_item is None
        or pull_request is None
        or projection.state is not ExecutionState.ROADMAP_UPDATE_REQUIRED
    ):
        raise ValueError(
            "ROADMAP_UPDATE_REQUIRED idempotency requires immutable merged PR evidence"
        )
    merged_identity = pull_request.merged_at or pull_request.updated_at or "merged"
    return _bounded_idempotency_key(
        "execution:"
        f"{project.project_id}:{work_item.key}:DEV:ROADMAP_RECONCILE:"
        f"pr{pull_request.number}:{pull_request.head_sha}:{merged_identity}:v1"
    )


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


def hotfix_publication_follow_up_key(project: Project, projection: ExecutionProjection) -> str:
    """One publication recovery handoff per actually merged hotfix delivery."""
    work_item, pr = projection.work_item, projection.pull_request
    context = project.delivery_context_for(work_item.key) if work_item is not None else None
    if (work_item is None or pr is None or context is None
            or context.mode is not DeliveryMode.HOTFIX or not pr.merged
            or projection.state is not ExecutionState.MERGED):
        raise ValueError("Publication follow-up requires a merged accepted HOTFIX")
    raw = (f"execution:{project.project_id}:{work_item.key}:DEV:HOTFIX_ARTIFACT:"
           f"pr{pr.number}:{pr.merge_commit_sha or pr.head_sha}:"
           f"{context.fingerprint()}:v1")
    return _bounded_idempotency_key(raw)


def build_hotfix_publication_follow_up(
    project: Project, projection: ExecutionProjection,
) -> str:
    """Same logical DEV session; publication must be separately proven."""
    work_item, pr = projection.work_item, projection.pull_request
    context = project.delivery_context_for(work_item.key) if work_item is not None else None
    if (work_item is None or pr is None or context is None
            or context.mode is not DeliveryMode.HOTFIX or not pr.merged
            or projection.state is not ExecutionState.MERGED):
        raise ValueError("Publication follow-up requires a merged accepted HOTFIX")
    return f"""Le hotfix {work_item.key} est fusionné dans la release, mais sa version testable n'est pas encore vérifiée.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR hotfix : #{pr.number}
Base release acceptée : {context.release_branch}
Head PR livré : {pr.head_sha}
Commit GitHub intégré : {pr.merge_commit_sha or 'indisponible — à retrouver sur GitHub'}

Reprends la même session DEV pour **uniquement** terminer ou diagnostiquer
la publication de la release corrigée. Relis AGENTS.md et le contrat immuable.
Revalide la PR fusionnée, le commit réellement intégré et l'ascendance
de la release actuelle (elle peut avoir avancé depuis le merge).
Vérifie la compatibilité SQL Server et les exigences de rollback en tant
que gate humaine : ne migre ni ne déploie rien automatiquement.
Utilise le workflow de publication explicitement autorisé dans le dépôt
consommateur; ne fabrique pas de digest ou de tag et n'improvise pas un
nouveau hotfix, un merge ni un forward-port.
Obtiens le tag correctif, SHA exact, image@sha256:digest vérifié côté
registre et résultat de validation sur le commit intégré. Si une
preuve manque, rapporte le blocage; la fusion seule ne termine pas le
WorkItem. Après livraison des preuves, ARRÊTE ton tour DEV; le cockpit
réconciliera seulement sur preuve GitHub complète.
""" + dev_target_instructions(context)
