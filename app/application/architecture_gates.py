from __future__ import annotations

from hashlib import sha256

from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    UnitOfWorkFactory,
    create_prompt_dispatch,
)
from app.application.roadmaps import RoadmapIssueReader, read_project_roadmap
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole, PromptDispatchStatus
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType
from app.domain.scheduler import SchedulerNextAction, derive_scheduler_projection


class ArchitectureGateAuthorizationError(ValueError):
    code = "ARCHITECTURE_GATE_AUTHORIZATION_FAILED"


class ArchitectureGateNotFound(ArchitectureGateAuthorizationError):
    code = "ARCHITECTURE_GATE_NOT_FOUND"


class ArchitectureGateWrongType(ArchitectureGateAuthorizationError):
    code = "NOT_ARCHITECTURE_GATE"


class ArchitectureGateNotReady(ArchitectureGateAuthorizationError):
    code = "ARCHITECTURE_GATE_NOT_READY"


class ArchitectureGateDispatchUnavailable(ArchitectureGateAuthorizationError):
    code = "ARCHITECTURE_GATE_DISPATCH_UNAVAILABLE"


def authorize_architecture_gate(
    project: Project,
    work_item_id: str,
    *,
    roadmap_reader: RoadmapIssueReader,
    uow_factory: UnitOfWorkFactory,
) -> PromptDispatch:
    """Create the ARCH prompt only after this explicit application command is invoked."""

    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    if not roadmap.pipeline.valid:
        raise ArchitectureGateNotReady("canonical roadmap is invalid")

    work_item = next(
        (item for item in roadmap.pipeline.work_items if item.key == work_item_id),
        None,
    )
    if work_item is None:
        raise ArchitectureGateNotFound(work_item_id)
    if work_item.type is not WorkItemType.ARCHITECTURE_GATE:
        raise ArchitectureGateWrongType(work_item_id)
    if work_item.status is not WorkItemStatus.READY:
        raise ArchitectureGateNotReady(work_item_id)

    scheduler = derive_scheduler_projection(roadmap.pipeline)
    scheduler_item = next(
        (item for item in scheduler.items if item.work_item.key == work_item_id),
        None,
    )
    if (
        scheduler_item is None
        or not scheduler_item.executable
        or scheduler_item.expected_role != "ARCH"
        or scheduler_item.next_action is not SchedulerNextAction.START_ARCH
    ):
        raise ArchitectureGateNotReady(work_item_id)

    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id=project.project_id,
            work_item_id=work_item.key,
            role=PromptDispatchRole.ARCH,
            prompt_text=build_architecture_gate_prompt(project, work_item),
            idempotency_key=architecture_gate_authorization_idempotency_key(\n                project,\n                work_item.key,\n            ),
        ),
        uow_factory=uow_factory,
    )
    if dispatch.status is not PromptDispatchStatus.PREPARED:
        raise ArchitectureGateDispatchUnavailable(work_item_id)
    return dispatch


def build_architecture_gate_prompt(project: Project, work_item: WorkItem) -> str:
    return f"""Tu travailles sur le dépôt GitHub `{project.repository_full_name}`.

Je veux une analyse architecturale approfondie en lecture seule pour la gate :

**{work_item.key} — {work_item.title}**

liée à l'issue parent :

**{work_item.parent}**

Contexte canonique :
- Project : {project.project_id}
- WorkItem : {work_item.key}
- Roadmap maître : #{project.roadmap_issue_number}
- Statut canonique : READY
- Autorisation humaine : accordée explicitement dans DevCockpit

Important :
- travaille sur le main actuel et synchronise-toi avec le vrai main avant l'analyse;
- ne modifie aucun fichier;
- ne crée aucune branche;
- ne crée aucun commit;
- ne crée aucune PR;
- ne modifie aucune issue GitHub ni le roadmap;
- lis d'abord AGENTS.md;
- consulte le roadmap maître #{project.roadmap_issue_number} et son bloc canonique présent;
- consulte {work_item.parent} au complet ainsi que les ADR réellement applicables;
- inspecte le code et les tests existants nécessaires à l'analyse;
- ne commence pas la tranche d'implémentation dépendante.

Produis les constats architecturaux, les contraintes, les décisions ou options réellement nécessaires et la recommandation permettant à l'humain de poursuivre la gate.
"""


def _authorization_idempotency_key(project: Project, work_item: WorkItem) -> str:
    raw = f"architecture-gate:{project.project_id}:{work_item.key}:ARCH:HUMAN_AUTHORIZED:v1"
    if len(raw) <= 200:
        return raw
    return f"architecture-gate:{sha256(raw.encode('utf-8')).hexdigest()}:v1"
