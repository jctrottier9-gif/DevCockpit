from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from app.application.handoffs import read_orchestration
from app.application.interaction_summaries import interaction_indication, read_interaction_summary
from app.application.parallel_executions import (
    ParallelDevExecutionProjection,
    read_project_parallel_dev_executions,
)
from app.domain.execution import ExecutionState
from app.domain.roadmap import WorkItemStatus, WorkItemType
from app.domain.roadmap_change import ApplicationStatus, ProposalStatus


class AttentionLevel(StrEnum):
    ACTION = "ACTION"
    WATCH = "WATCH"


class AttentionState(StrEnum):
    ACTION = "ACTION"
    WATCH = "WATCH"
    CLEAR = "CLEAR"


class AttentionKind(StrEnum):
    PROMPT_READY = "PROMPT_READY"
    TRANSPORT_BLOCKED = "TRANSPORT_BLOCKED"
    CI_RED = "CI_RED"
    HANDOFF = "HANDOFF"
    ROADMAP_UPDATE_REQUIRED = "ROADMAP_UPDATE_REQUIRED"
    ROADMAP_PROPOSAL = "ROADMAP_PROPOSAL"
    ROADMAP_APPLICATION = "ROADMAP_APPLICATION"
    RESOURCE_LOCK_CONFLICT = "RESOURCE_LOCK_CONFLICT"
    ARCHITECTURE_GATE_AUTHORIZATION = "ARCHITECTURE_GATE_AUTHORIZATION"
    CHATGPT_SEND = "CHATGPT_SEND"
    PR_FINALIZATION = "PR_FINALIZATION"


@dataclass(frozen=True, slots=True)
class AttentionAction:
    kind: str
    label: str
    target: str
    work_item_id: str | None = None
    href: str | None = None
    dispatch_id: str | None = None
    handoff_id: str | None = None
    proposal_id: str | None = None
    application_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttentionEvidence:
    source: str
    identity: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttentionItem:
    stable_key: str
    level: AttentionLevel
    kind: AttentionKind
    title: str
    reason: str
    project_id: str
    work_item_id: str | None
    role: str | None
    agent_session: str | None
    primary_action: AttentionAction
    pr_number: int | None = None
    pr_url: str | None = None
    evidence: tuple[AttentionEvidence, ...] = ()
    context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AttentionProjection:
    state: AttentionState
    action_count: int
    watch_count: int
    items: tuple[AttentionItem, ...]


_LEVEL_ORDER = {
    AttentionLevel.ACTION: 0,
    AttentionLevel.WATCH: 1,
}


def _stable_key(project_id: str, role: str | None, work_item_id: str | None, action: str) -> str:
    return ":".join(
        (
            project_id,
            role or "-",
            work_item_id or "-",
            action,
        )
    )


def _merge_item(existing: AttentionItem, incoming: AttentionItem) -> AttentionItem:
    level = (
        AttentionLevel.ACTION
        if AttentionLevel.ACTION in {existing.level, incoming.level}
        else AttentionLevel.WATCH
    )
    reasons = tuple(
        dict.fromkeys(
            part
            for part in (existing.reason, incoming.reason)
            if part
        )
    )
    evidence = {
        (item.source, item.identity, item.detail): item
        for item in (*existing.evidence, *incoming.evidence)
    }
    context = dict(existing.context or {})
    for key, value in (incoming.context or {}).items():
        if key not in context or context[key] in (None, "", [], {}):
            context[key] = value
    transport_blocks = incoming.kind is AttentionKind.TRANSPORT_BLOCKED
    prompt_override = incoming.primary_action.dispatch_id is not None
    actionable_override = transport_blocks or prompt_override
    return replace(
        existing,
        level=level,
        kind=incoming.kind if actionable_override else existing.kind,
        title=incoming.title if actionable_override else existing.title,
        primary_action=(
            incoming.primary_action if actionable_override else existing.primary_action
        ),
        reason=" · ".join(reasons),
        evidence=tuple(
            evidence[key]
            for key in sorted(evidence)
        ),
        context=context or None,
        pr_number=existing.pr_number or incoming.pr_number,
        pr_url=existing.pr_url or incoming.pr_url,
    )


def _add(items: dict[str, AttentionItem], item: AttentionItem) -> None:
    current = items.get(item.stable_key)
    items[item.stable_key] = item if current is None else _merge_item(current, item)


def _orchestration_action(
    *,
    project_id: str,
    work_item_id: str,
    role: str,
    action_kind: str,
    label: str,
    kind: AttentionKind,
    title: str,
    reason: str,
    handoff_id: object | None = None,
    proposal_id: object | None = None,
    application_id: object | None = None,
    level: AttentionLevel = AttentionLevel.ACTION,
    evidence_source: str,
    evidence_identity: object,
) -> AttentionItem:
    return AttentionItem(
        stable_key=_stable_key(project_id, role, work_item_id, action_kind),
        level=level,
        kind=kind,
        title=title,
        reason=reason,
        project_id=project_id,
        work_item_id=work_item_id,
        role=role,
        agent_session=f"{project_id}:{role}:{work_item_id}",
        primary_action=AttentionAction(
            kind=action_kind,
            label=label,
            target="orchestration",
            work_item_id=work_item_id,
            handoff_id=str(handoff_id) if handoff_id is not None else None,
            proposal_id=str(proposal_id) if proposal_id is not None else None,
            application_id=str(application_id) if application_id is not None else None,
        ),
        evidence=(
            AttentionEvidence(
                evidence_source,
                str(evidence_identity),
            ),
        ),
    )


def _execution_items(
    project_id: str,
    projection: ParallelDevExecutionProjection,
    items: dict[str, AttentionItem],
) -> dict[str, object]:
    by_work_item: dict[str, object] = {}
    for candidate in projection.items:
        work_item = candidate.execution.work_item
        if work_item is None:
            continue
        by_work_item[work_item.key] = candidate
        pull_request = candidate.execution.pull_request
        github_watchdog = candidate.github_watchdog
        if github_watchdog is not None:
            overdue = github_watchdog.due
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        f"GITHUB_WATCHDOG_{github_watchdog.kind.value}",
                    ),
                    level=AttentionLevel.ACTION if overdue else AttentionLevel.WATCH,
                    kind=AttentionKind.PR_FINALIZATION,
                    title=f"DEV · {work_item.key} · watchdog {github_watchdog.kind.value}",
                    reason=(
                        f"Dernière activité GitHub {github_watchdog.last_activity_at}; "
                        f"seuil {round(github_watchdog.threshold_seconds)} s; "
                        f"échéance {github_watchdog.deadline_at.isoformat()}; "
                        f"récupération {github_watchdog.recovery_state}."
                    ),
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind=github_watchdog.kind.value,
                        label="Ouvrir la PR",
                        target="pull_request" if pull_request and pull_request.url else "orchestration",
                        work_item_id=work_item.key,
                        href=pull_request.url if pull_request else None,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "GitHubWaitWatchdog",
                            github_watchdog.evidence_identity,
                            github_watchdog.recovery_state,
                        ),
                    ),
                    context={
                        "watchdog_kind": github_watchdog.kind.value,
                        "last_activity_at": github_watchdog.last_activity_at,
                        "threshold_seconds": github_watchdog.threshold_seconds,
                        "deadline_at": github_watchdog.deadline_at.isoformat(),
                        "due": github_watchdog.due,
                        "recovery_prepared": github_watchdog.recovery_prepared,
                        "recovery_state": github_watchdog.recovery_state,
                    },
                ),
            )

        if candidate.execution.state is ExecutionState.CI_RED:
            action_kind = "FIX_CI"
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(project_id, "DEV", work_item.key, action_kind),
                    level=AttentionLevel.ACTION,
                    kind=AttentionKind.CI_RED,
                    title=f"DEV · {work_item.key} · CI rouge",
                    reason="La projection d'exécution exige une correction de la CI.",
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind=action_kind,
                        label="Ouvrir la PR et corriger la CI",
                        target="pull_request" if pull_request and pull_request.url else "orchestration",
                        work_item_id=work_item.key,
                        href=pull_request.url if pull_request else None,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "ExecutionProjection",
                            f"{work_item.key}:CI_RED",
                            "GitHub current-head CI is red",
                        ),
                    ),
                    context={
                        "failed_jobs": list(candidate.execution.ci.failed_jobs)
                        if candidate.execution.ci is not None
                        else [],
                    },
                ),
            )

        if candidate.execution.state in {
            ExecutionState.BRANCH_SYNC_BLOCKED,
            ExecutionState.MERGE_BLOCKED,
        }:
            blocked_state = candidate.execution.state.value
            reason = (
                candidate.execution.diagnostics[-1].message
                if candidate.execution.diagnostics
                else "GitHub refused deterministic PR finalization."
            )
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        blocked_state,
                    ),
                    level=AttentionLevel.ACTION,
                    kind=AttentionKind.PR_FINALIZATION,
                    title=f"DEV · {work_item.key} · {blocked_state}",
                    reason=reason,
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind=blocked_state,
                        label="Ouvrir la PR et examiner le blocage",
                        target="pull_request" if pull_request and pull_request.url else "orchestration",
                        work_item_id=work_item.key,
                        href=pull_request.url if pull_request else None,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "ExecutionProjection",
                            f"{work_item.key}:{blocked_state}",
                            reason,
                        ),
                    ),
                ),
            )

        if candidate.execution.state is ExecutionState.BASE_OUTDATED:
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        "SYNC_BRANCH",
                    ),
                    level=AttentionLevel.WATCH,
                    kind=AttentionKind.PR_FINALIZATION,
                    title=f"DEV · {work_item.key} · branche derrière la base",
                    reason=(
                        "DevCockpit synchronise mécaniquement la branche avec la base observée "
                        "avant de réévaluer la CI."
                    ),
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind="SYNC_BRANCH",
                        label="Ouvrir la PR",
                        target="pull_request" if pull_request and pull_request.url else "orchestration",
                        work_item_id=work_item.key,
                        href=pull_request.url if pull_request else None,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "ExecutionProjection",
                            f"{work_item.key}:BASE_OUTDATED",
                        ),
                    ),
                ),
            )

        if (
            candidate.execution.state is ExecutionState.READY_TO_MERGE
            and candidate.execution.next_action.value in {"WAIT_AUTO_MERGE", "MERGE_PR"}
        ):
            waiting_on_github = candidate.execution.next_action.value == "WAIT_AUTO_MERGE"
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        candidate.execution.next_action.value,
                    ),
                    level=AttentionLevel.WATCH,
                    kind=AttentionKind.PR_FINALIZATION,
                    title=f"DEV · {work_item.key} · PR prête à finaliser",
                    reason=(
                        "L'auto-merge est armé; GitHub est responsable du merge."
                        if waiting_on_github
                        else "DevCockpit finalisera le merge après revalidation des preuves courantes."
                    ),
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind=candidate.execution.next_action.value,
                        label="Ouvrir la PR",
                        target="pull_request" if pull_request and pull_request.url else "orchestration",
                        work_item_id=work_item.key,
                        href=pull_request.url if pull_request else None,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "ExecutionProjection",
                            f"{work_item.key}:{candidate.execution.next_action.value}",
                        ),
                    ),
                ),
            )

        if candidate.execution.state is ExecutionState.ROADMAP_UPDATE_REQUIRED:
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        "RECONCILE_ROADMAP",
                    ),
                    level=AttentionLevel.ACTION,
                    kind=AttentionKind.ROADMAP_UPDATE_REQUIRED,
                    title=f"DEV · {work_item.key} · roadmap à réconcilier",
                    reason=(
                        "La livraison GitHub est fusionnée et verte, mais le roadmap "
                        "canonique n'est pas encore réconcilié."
                    ),
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind="RECONCILE_ROADMAP",
                        label="Ouvrir l'orchestration du WorkItem",
                        target="orchestration",
                        work_item_id=work_item.key,
                    ),
                    pr_number=pull_request.number if pull_request else None,
                    pr_url=pull_request.url if pull_request else None,
                    evidence=(
                        AttentionEvidence(
                            "ExecutionProjection",
                            f"{work_item.key}:ROADMAP_UPDATE_REQUIRED",
                        ),
                    ),
                ),
            )

        conflict = candidate.lock_conflict
        if conflict is not None:
            _add(
                items,
                AttentionItem(
                    stable_key=_stable_key(
                        project_id,
                        "DEV",
                        work_item.key,
                        "RESOLVE_RESOURCE_LOCK",
                    ),
                    level=AttentionLevel.ACTION,
                    kind=AttentionKind.RESOURCE_LOCK_CONFLICT,
                    title=f"DEV · {work_item.key} · conflit ResourceLock",
                    reason=(
                        f"{conflict.surface.key} [{conflict.requested_mode.value}] "
                        f"est détenue par {conflict.holder_work_item_id} "
                        f"({conflict.holder_agent_session})."
                    ),
                    project_id=project_id,
                    work_item_id=work_item.key,
                    role="DEV",
                    agent_session=candidate.agent_session,
                    primary_action=AttentionAction(
                        kind="RESOLVE_RESOURCE_LOCK",
                        label="Ouvrir le WorkItem bloqué",
                        target="orchestration",
                        work_item_id=work_item.key,
                    ),
                    evidence=(
                        AttentionEvidence(
                            "ResourceLock",
                            f"{work_item.key}:{conflict.surface.key}",
                            conflict.reason,
                        ),
                    ),
                    context={
                        "surface": conflict.surface.key,
                        "requested_mode": conflict.requested_mode.value,
                        "holder_work_item_id": conflict.holder_work_item_id,
                        "holder_agent_session": conflict.holder_agent_session,
                        "holder_mode": conflict.holder_mode.value,
                        "holder_state": conflict.holder_state.value,
                        "reason": conflict.reason,
                    },
                ),
            )
    return by_work_item


def _architecture_gate_items(
    *,
    project_id: str,
    scheduler,
    prepared_dispatches,
    items: dict[str, AttentionItem],
) -> set[str]:
    """Surface READY ARCH gates without ever creating their PromptDispatch."""

    authorized: set[str] = set()
    prepared_arch = {
        dispatch.work_item_id
        for dispatch in prepared_dispatches
        if dispatch.project_id == project_id and dispatch.role.value == "ARCH"
    }

    for candidate in scheduler.items:
        work_item = candidate.work_item
        if (
            work_item.type is not WorkItemType.ARCHITECTURE_GATE
            or work_item.status is not WorkItemStatus.READY
            or not candidate.executable
            or candidate.expected_role != "ARCH"
        ):
            continue

        if work_item.key in prepared_arch:
            authorized.add(work_item.key)
            continue

        _add(
            items,
            AttentionItem(
                stable_key=_stable_key(
                    project_id,
                    "ARCH",
                    work_item.key,
                    "AUTHORIZE_ARCHITECTURE_GATE",
                ),
                level=AttentionLevel.ACTION,
                kind=AttentionKind.ARCHITECTURE_GATE_AUTHORIZATION,
                title=f"ARCH · {work_item.key} · autorisation requise",
                reason=(
                    "La gate architecturale est READY et ses dépendances sont satisfaites, "
                    "mais DevCockpit ne peut pas lancer une analyse ARCH sans autorisation humaine explicite."
                ),
                project_id=project_id,
                work_item_id=work_item.key,
                role="ARCH",
                agent_session=f"{project_id}:ARCH:{work_item.key}",
                primary_action=AttentionAction(
                    kind="AUTHORIZE_ARCHITECTURE_GATE",
                    label="Autoriser l'analyse architecturale",
                    target="architecture_gate",
                    work_item_id=work_item.key,
                ),
                evidence=(
                    AttentionEvidence(
                        "SchedulerProjection",
                        work_item.key,
                        "READY_ARCHITECTURE_GATE_REQUIRES_HUMAN_AUTHORIZATION",
                    ),
                ),
                context={
                    "human_authorization_required": True,
                    "prompt_dispatch_created": False,
                },
            ),
        )
    return authorized


def _prompt_items(
    *,
    project_id: str,
    prepared_dispatches,
    responded_delivery_ids: set[object],
    uow_factory,
    execution_by_work_item: dict[str, object],
    eligible_work_items: set[str],
    companion_connected: bool,
    items: dict[str, AttentionItem],
) -> None:
    for dispatch in prepared_dispatches:
        if dispatch.project_id != project_id or dispatch.work_item_id not in eligible_work_items:
            continue
        with uow_factory() as uow:
            delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
            interaction = read_interaction_summary(dispatch, uow=uow)
        if delivery is not None and delivery.delivery_id in responded_delivery_ids:
            continue

        role = dispatch.role.value
        execution_item = execution_by_work_item.get(dispatch.work_item_id)
        is_ci_red = (
            role == "DEV"
            and execution_item is not None
            and execution_item.execution.state is ExecutionState.CI_RED
        )
        is_roadmap_reconcile = (
            role == "DEV"
            and execution_item is not None
            and execution_item.execution.state
            is ExecutionState.ROADMAP_UPDATE_REQUIRED
        )
        action_kind = (
            "FIX_CI"
            if is_ci_red
            else "RECONCILE_ROADMAP"
            if is_roadmap_reconcile
            else "SEND_PROMPT"
        )
        blocked_by_transport = (
            not companion_connected
            and (delivery is None or not delivery.is_acknowledged)
        )
        level = AttentionLevel.ACTION
        send_state = interaction.send_state

        if send_state in {"AMBIGUOUS", "BLOCKED"}:
            primary_action = AttentionAction(
                kind="RECONCILE_CHATGPT_SEND",
                label=(
                    "Vérifier l'envoi ChatGPT avant toute reprise"
                    if send_state == "AMBIGUOUS"
                    else "Corriger le blocage d'envoi ChatGPT"
                ),
                target="companion",
                work_item_id=dispatch.work_item_id,
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = AttentionKind.CHATGPT_SEND
            reason = (
                "L'envoi a franchi SEND_ARMED mais sa confirmation est incertaine; "
                "aucun renvoi automatique n'est permis."
                if send_state == "AMBIGUOUS"
                else "L'envoi ChatGPT est bloqué avant la barrière irréversible."
            )
            action_kind = "RECONCILE_CHATGPT_SEND"
        elif interaction.manual_send_required:
            primary_action = AttentionAction(
                kind="LAUNCH_ARCH_MANUALLY",
                label="Lancer ASTRA manuellement dans Firefox",
                target="companion",
                work_item_id=dispatch.work_item_id,
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = AttentionKind.CHATGPT_SEND
            reason = (
                "Le prompt ARCH est reçu par Firefox. La gate reste manuelle: "
                "sélectionner une conversation ChatGPT déjà placée en Work mode et la lancer "
                "explicitement depuis l'extension."
            )
            action_kind = "LAUNCH_ARCH_MANUALLY"
        elif send_state == "SENT_CONFIRMED":
            level = AttentionLevel.WATCH
            primary_action = AttentionAction(
                kind="WAIT_IMPORTED_RESPONSE",
                label="Ouvrir l'orchestration · aucune réponse importée",
                target="orchestration",
                work_item_id=dispatch.work_item_id,
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = AttentionKind.CHATGPT_SEND
            reason = (
                "L'envoi ChatGPT est confirmé. Aucune réponse n'a encore été importée; "
                "cela ne prouve ni génération en cours ni fin du travail."
            )
            action_kind = "WAIT_IMPORTED_RESPONSE"
        elif send_state is not None and delivery is not None and delivery.is_acknowledged:
            level = AttentionLevel.WATCH
            primary_action = AttentionAction(
                kind="WAIT_CHATGPT_SEND",
                label="Suivre l'état d'envoi ChatGPT",
                target="companion",
                work_item_id=dispatch.work_item_id,
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = AttentionKind.CHATGPT_SEND
            if send_state == "SEND_ARMED":
                reason = (
                    "L'envoi est armé et attend une confirmation ciblée; aucun second envoi "
                    "ne doit être déclenché pendant cette phase."
                )
            elif send_state == "RETRYABLE_FAILURE":
                reason = (
                    "Un échec certain avant SEND_ARMED est observé; la reprise automatique "
                    "reste bornée par le companion."
                )
            else:
                reason = (
                    f"État d'envoi ChatGPT observé: {send_state}. "
                    "Aucune action humaine n'est requise tant qu'il n'est pas bloqué ou ambigu."
                )
            action_kind = "WAIT_CHATGPT_SEND"
        elif blocked_by_transport:
            primary_action = AttentionAction(
                kind="CONNECT_COMPANION",
                label="Reconnecter le companion Firefox",
                target="companion",
                work_item_id=dispatch.work_item_id,
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = AttentionKind.TRANSPORT_BLOCKED
            reason = (
                "Un prompt est prêt, mais aucun ACK durable n'existe et le companion "
                "Firefox est déconnecté; la livraison est donc bloquée."
            )
        else:
            primary_action = AttentionAction(
                kind=action_kind,
                label=(
                    "Ouvrir la PR et suivre le prompt correctif"
                    if is_ci_red
                    else "Ouvrir l'orchestration et suivre la réconciliation"
                    if is_roadmap_reconcile
                    else f"Ouvrir l'orchestration et suivre le prompt {role}"
                ),
                target=(
                    "pull_request"
                    if is_ci_red
                    and execution_item.execution.pull_request is not None
                    and execution_item.execution.pull_request.url
                    else "orchestration"
                ),
                work_item_id=dispatch.work_item_id,
                href=(
                    execution_item.execution.pull_request.url
                    if is_ci_red and execution_item.execution.pull_request is not None
                    else None
                ),
                dispatch_id=str(dispatch.dispatch_id),
            )
            kind = (
                AttentionKind.CI_RED
                if is_ci_red
                else AttentionKind.ROADMAP_UPDATE_REQUIRED
                if is_roadmap_reconcile
                else AttentionKind.PROMPT_READY
            )
            reason = (
                "Le follow-up DEV de correction est déjà préparé pour la CI rouge."
                if is_ci_red
                else (
                    "Le follow-up DEV de réconciliation post-merge est préparé; "
                    "il mettra directement le roadmap GitHub à jour sans confirmation humaine."
                    if is_roadmap_reconcile
                    else (
                        "Le prompt est reçu par Firefox; aucun état ChatGPT plus avancé "
                        "n'est encore observé."
                        if delivery is not None and delivery.is_acknowledged
                        else "Le PromptDispatch est préparé et attend sa livraison au companion Firefox."
                    )
                )
            )

        pull_request = (
            execution_item.execution.pull_request
            if (is_ci_red or is_roadmap_reconcile) and execution_item is not None
            else None
        )
        _add(
            items,
            AttentionItem(
                stable_key=_stable_key(
                    project_id,
                    role,
                    dispatch.work_item_id,
                    action_kind,
                ),
                level=level,
                kind=kind,
                title=(
                    f"DEV · {dispatch.work_item_id} · CI rouge"
                    if is_ci_red
                    else f"DEV · {dispatch.work_item_id} · réconciliation roadmap prête"
                    if is_roadmap_reconcile
                    else f"{role} · {dispatch.work_item_id} · {interaction_indication(interaction)}"
                ),
                reason=reason,
                project_id=project_id,
                work_item_id=dispatch.work_item_id,
                role=role,
                agent_session=dispatch.agent_session,
                primary_action=primary_action,
                pr_number=pull_request.number if pull_request else None,
                pr_url=pull_request.url if pull_request else None,
                evidence=(
                    AttentionEvidence(
                        "PromptDispatch",
                        str(dispatch.dispatch_id),
                        "PREPARED",
                    ),
                ),
                context={
                    "dispatch_id": str(dispatch.dispatch_id),
                    "delivery_id": str(delivery.delivery_id) if delivery is not None else None,
                    "delivery_acknowledged": (
                        delivery.is_acknowledged if delivery is not None else False
                    ),
                    "transport_connected": companion_connected,
                    "interaction": asdict(interaction),
                    "interaction_state": interaction.state,
                    "chatgpt_send_state": interaction.send_state,
                    "chatgpt_send_attempt": interaction.send_attempt_count,
                    "chatgpt_send_error": interaction.send_error_code,
                    "manual_send_required": interaction.manual_send_required,
                    "automatic_resend_allowed": interaction.automatic_resend_allowed,
                    "imported_response_available": interaction.imported_response_available,
                },
            ),
        )


def _handoff_and_roadmap_items(
    *,
    project,
    work_item_ids: set[str],
    roadmap_reader,
    evidence_reader,
    uow_factory,
    items: dict[str, AttentionItem],
) -> None:
    for work_item_id in sorted(work_item_ids):
        view = read_orchestration(
            project,
            work_item_id,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
        )
        for handoff in view["handoffs"]:
            role = str(handoff["target_role"])
            actions = handoff["actions"]
            handoff_id = handoff["handoff_id"]
            responses = handoff["responses"]

            if actions["accept"]:
                _add(
                    items,
                    _orchestration_action(
                        project_id=project.project_id,
                        work_item_id=work_item_id,
                        role=role,
                        action_kind="REVIEW_DECISION",
                        label=f"Examiner la réponse {role}",
                        kind=AttentionKind.HANDOFF,
                        title=f"{role} · {work_item_id} · décision à accepter",
                        reason=(
                            f"{len(responses)} réponse(s) retournée(s) sont disponibles "
                            "pour cette consultation."
                        ),
                        handoff_id=handoff_id,
                        evidence_source="Handoff",
                        evidence_identity=handoff_id,
                    ),
                )
            elif actions["transfer_to_po"]:
                _add(
                    items,
                    _orchestration_action(
                        project_id=project.project_id,
                        work_item_id=work_item_id,
                        role="PO",
                        action_kind="CONSULT_PO",
                        label="Consulter le Product Owner",
                        kind=AttentionKind.HANDOFF,
                        title=f"PO · {work_item_id} · consultation requise",
                        reason=(
                            "La consultation Architecte peut maintenant être transférée "
                            "explicitement au Product Owner."
                        ),
                        handoff_id=handoff_id,
                        evidence_source="Handoff",
                        evidence_identity=handoff_id,
                    ),
                )

            if actions["create_proposal"] and not handoff["proposals"]:
                _add(
                    items,
                    _orchestration_action(
                        project_id=project.project_id,
                        work_item_id=work_item_id,
                        role="PO",
                        action_kind="CREATE_ROADMAP_PROPOSAL",
                        label="Créer la proposal de roadmap",
                        kind=AttentionKind.ROADMAP_PROPOSAL,
                        title=f"PO · {work_item_id} · redécoupage à matérialiser",
                        reason=(
                            "Une SCOPE_DECISION tenue pour autorisation permet de créer "
                            "explicitement une RoadmapChangeProposal."
                        ),
                        handoff_id=handoff_id,
                        evidence_source="Decision",
                        evidence_identity=handoff["decision"]["decision_id"],
                    ),
                )

            for proposal in handoff["proposals"]:
                proposal_id = proposal["proposal_id"]
                applications = proposal["applications"]
                statuses = {str(application["status"]) for application in applications}

                for application in applications:
                    application_id = application["application_id"]
                    status = str(application["status"])
                    if status == ApplicationStatus.RECONCILIATION_REQUIRED.value:
                        _add(
                            items,
                            _orchestration_action(
                                project_id=project.project_id,
                                work_item_id=work_item_id,
                                role="PO",
                                action_kind="RECONCILE_ROADMAP",
                                label="Réconcilier l'application roadmap",
                                kind=AttentionKind.ROADMAP_APPLICATION,
                                title=f"PO · {work_item_id} · réconciliation GitHub requise",
                                reason=(
                                    "Le résultat distant d'une application roadmap est incertain "
                                    "et doit être relu explicitement sans nouveau PATCH."
                                ),
                                proposal_id=proposal_id,
                                application_id=application_id,
                                evidence_source="RoadmapChangeApplication",
                                evidence_identity=application_id,
                            ),
                        )
                    elif status == ApplicationStatus.CONFLICT.value:
                        _add(
                            items,
                            _orchestration_action(
                                project_id=project.project_id,
                                work_item_id=work_item_id,
                                role="PO",
                                action_kind="REVIEW_ROADMAP_CONFLICT",
                                label="Examiner le conflit roadmap",
                                kind=AttentionKind.ROADMAP_APPLICATION,
                                title=f"PO · {work_item_id} · conflit roadmap",
                                reason=(
                                    "Le roadmap distant a divergé; aucune réécriture automatique "
                                    "n'est autorisée."
                                ),
                                proposal_id=proposal_id,
                                application_id=application_id,
                                evidence_source="RoadmapChangeApplication",
                                evidence_identity=application_id,
                            ),
                        )
                    elif status in {
                        ApplicationStatus.PREPARED.value,
                        ApplicationStatus.APPLYING.value,
                    }:
                        _add(
                            items,
                            _orchestration_action(
                                project_id=project.project_id,
                                work_item_id=work_item_id,
                                role="PO",
                                action_kind="WAIT_ROADMAP_APPLICATION",
                                label="Ouvrir l'application roadmap",
                                kind=AttentionKind.ROADMAP_APPLICATION,
                                title=f"PO · {work_item_id} · application roadmap en cours",
                                reason=(
                                    "Une application roadmap est active; aucune nouvelle action "
                                    "humaine n'est utile avant son issue ou sa réconciliation."
                                ),
                                proposal_id=proposal_id,
                                application_id=application_id,
                                level=AttentionLevel.WATCH,
                                evidence_source="RoadmapChangeApplication",
                                evidence_identity=application_id,
                            ),
                        )

                proposal_status = str(proposal["status"])
                if proposal_status == ProposalStatus.DRAFT.value:
                    _add(
                        items,
                        _orchestration_action(
                            project_id=project.project_id,
                            work_item_id=work_item_id,
                            role="PO",
                            action_kind="REVIEW_ROADMAP_PROPOSAL",
                            label="Revoir la proposal de roadmap",
                            kind=AttentionKind.ROADMAP_PROPOSAL,
                            title=f"PO · {work_item_id} · proposal DRAFT à revoir",
                            reason=(
                                "La proposal locale est encore DRAFT et attend une action "
                                "explicite de preview, révision, confirmation ou annulation."
                            ),
                            proposal_id=proposal_id,
                            evidence_source="RoadmapChangeProposal",
                            evidence_identity=proposal_id,
                        ),
                    )
                elif (
                    proposal_status == ProposalStatus.CONFIRMED.value
                    and not statuses.intersection(
                        {
                            ApplicationStatus.PREPARED.value,
                            ApplicationStatus.APPLYING.value,
                            ApplicationStatus.RECONCILIATION_REQUIRED.value,
                            ApplicationStatus.CONFLICT.value,
                        }
                    )
                ):
                    _add(
                        items,
                        _orchestration_action(
                            project_id=project.project_id,
                            work_item_id=work_item_id,
                            role="PO",
                            action_kind="APPLY_ROADMAP",
                            label="Appliquer la proposal au roadmap",
                            kind=AttentionKind.ROADMAP_PROPOSAL,
                            title=f"PO · {work_item_id} · proposal confirmée",
                            reason=(
                                "La révision exacte est confirmée et l'action GitHub existante "
                                "peut être déclenchée explicitement."
                            ),
                            proposal_id=proposal_id,
                            evidence_source="RoadmapChangeProposal",
                            evidence_identity=proposal_id,
                        ),
                    )


def read_project_attention(
    project,
    *,
    roadmap_reader,
    evidence_reader,
    uow_factory,
    max_parallel_dev_executions: int,
    companion_connected: bool,
    dev_stale_after_seconds: float = 3600.0,
    pr_no_ci_after_seconds: float = 900.0,
    ci_stall_after_seconds: float = 1800.0,
    auto_merge_grace_seconds: float = 600.0,
) -> AttentionProjection:
    parallel = read_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
        uow_factory=uow_factory,
        max_parallel_dev_executions=max_parallel_dev_executions,
        dev_stale_after_seconds=dev_stale_after_seconds,
        pr_no_ci_after_seconds=pr_no_ci_after_seconds,
        ci_stall_after_seconds=ci_stall_after_seconds,
        auto_merge_grace_seconds=auto_merge_grace_seconds,
    )
    items: dict[str, AttentionItem] = {}
    execution_by_work_item = _execution_items(project.project_id, parallel, items)

    with uow_factory() as uow:
        prepared_dispatches = tuple(uow.prompt_dispatches.list_prepared())
        responses = tuple(uow.chatgpt_responses.list_all())
        handoffs = tuple(uow.handoffs.list_for_project(project.project_id))
        responded_delivery_ids = {response.delivery_id for response in responses}

    active_handoff_work_items = {
        handoff.work_item_id
        for handoff in handoffs
        if handoff.blocking
    }
    authorized_architecture_gates = _architecture_gate_items(
        project_id=project.project_id,
        scheduler=parallel.scheduler,
        prepared_dispatches=prepared_dispatches,
        items=items,
    )
    eligible_work_items = (
        set(execution_by_work_item)
        | active_handoff_work_items
        | authorized_architecture_gates
    )

    _prompt_items(
        project_id=project.project_id,
        prepared_dispatches=prepared_dispatches,
        responded_delivery_ids=responded_delivery_ids,
        uow_factory=uow_factory,
        execution_by_work_item=execution_by_work_item,
        eligible_work_items=eligible_work_items,
        companion_connected=companion_connected,
        items=items,
    )

    _handoff_and_roadmap_items(
        project=project,
        work_item_ids={handoff.work_item_id for handoff in handoffs},
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
        uow_factory=uow_factory,
        items=items,
    )

    ordered = tuple(
        sorted(
            items.values(),
            key=lambda item: (
                _LEVEL_ORDER[item.level],
                item.role or "",
                item.work_item_id or "",
                item.primary_action.kind,
                item.stable_key,
            ),
        )
    )
    action_count = sum(item.level is AttentionLevel.ACTION for item in ordered)
    watch_count = sum(item.level is AttentionLevel.WATCH for item in ordered)
    state = (
        AttentionState.ACTION
        if action_count
        else AttentionState.WATCH
        if watch_count
        else AttentionState.CLEAR
    )
    return AttentionProjection(
        state=state,
        action_count=action_count,
        watch_count=watch_count,
        items=ordered,
    )
