from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from app.application.handoffs import read_orchestration
from app.application.parallel_executions import (
    ParallelDevExecutionProjection,
    read_project_parallel_dev_executions,
)
from app.domain.execution import ExecutionState
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
    return replace(
        existing,
        level=level,
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
        if delivery is not None and delivery.delivery_id in responded_delivery_ids:
            continue

        role = dispatch.role.value
        execution_item = execution_by_work_item.get(dispatch.work_item_id)
        is_ci_red = (
            role == "DEV"
            and execution_item is not None
            and execution_item.execution.state is ExecutionState.CI_RED
        )
        action_kind = "FIX_CI" if is_ci_red else "SEND_PROMPT"
        blocked_by_transport = (
            not companion_connected
            and (delivery is None or not delivery.is_acknowledged)
        )
        if blocked_by_transport:
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
                    "Ouvrir la PR et envoyer le prompt correctif"
                    if is_ci_red
                    else f"Ouvrir l'orchestration et envoyer le prompt {role}"
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
            kind = AttentionKind.CI_RED if is_ci_red else AttentionKind.PROMPT_READY
            reason = (
                "Le follow-up DEV de correction est déjà préparé pour la CI rouge."
                if is_ci_red
                else (
                    "Le prompt est déjà accepté dans la file Firefox et attend l'envoi explicite."
                    if delivery is not None and delivery.is_acknowledged
                    else "Le PromptDispatch est préparé et peut être livré puis envoyé explicitement."
                )
            )

        pull_request = (
            execution_item.execution.pull_request
            if is_ci_red and execution_item is not None
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
                level=AttentionLevel.ACTION,
                kind=kind,
                title=(
                    f"DEV · {dispatch.work_item_id} · CI rouge"
                    if is_ci_red
                    else f"{role} · {dispatch.work_item_id} · prompt prêt"
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
) -> AttentionProjection:
    parallel = read_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
        uow_factory=uow_factory,
        max_parallel_dev_executions=max_parallel_dev_executions,
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
    eligible_work_items = set(execution_by_work_item) | active_handoff_work_items

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
