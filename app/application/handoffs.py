"""Explicit user commands. Imported prose is never interpreted as a command."""
from dataclasses import asdict, dataclass, replace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
import json

from app.application.executions import (
    _ci_red_idempotency_key,
    read_project_execution_for_work_item,
)
from app.application.interaction_summaries import (
    interaction_indication,
    read_interaction_summary,
)
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch_in_uow
from app.application.roadmaps import RoadmapSourceError, read_project_roadmap
from app.domain.execution import ExecutionState
from app.domain.handoff import (
    Decision,
    DecisionEffect,
    DecisionType,
    Handoff,
    HandoffPurpose,
    HandoffStatus,
    OrchestrationConflict,
    WritebackRiskAuthorization,
    utc_now,
    validate_decision_kind,
    validate_handoff_kind,
)
from app.domain.prompt_dispatch import PromptDispatchRole, PromptDispatchStatus, build_agent_session
from app.domain.roadmap import WorkItemType
from app.domain.scheduler import derive_scheduler_projection


@dataclass(frozen=True)
class CreateHandoff:
    creation_command_id: UUID
    source_dispatch_id: UUID
    question: str
    context: str
    created_by: str
    source_response_id: UUID | None = None
    target_role: str = "ARCH"
    purpose: str = "TECHNICAL_GUIDANCE"


@dataclass(frozen=True)
class TransferHandoffToPO:
    transfer_command_id: UUID
    expected_version: int
    question: str
    context: str
    created_by: str
    purpose: str = "PRODUCT_CLARIFICATION"
    source_response_id: UUID | None = None


@dataclass(frozen=True)
class AcceptDecision:
    acceptance_command_id: UUID
    expected_version: int
    source_response_id: UUID
    summary: str
    effect: DecisionEffect
    accepted_by: str
    decision_type: str = "ARCHITECTURE_GUIDANCE"
    accepts_residual_writeback_risk: bool = False


@dataclass(frozen=True)
class CancelHandoff:
    cancellation_command_id: UUID
    expected_version: int
    cancelled_by: str
    reason: str


def _require_handoff(uow, identity):
    handoff = uow.handoffs.get(identity)
    if handoff is None:
        raise OrchestrationConflict("Handoff not found")
    return handoff


def _validate_dispatch(dispatch, project_id, work_item_id, role):
    if (
        dispatch is None
        or dispatch.project_id != project_id
        or dispatch.work_item_id != work_item_id
        or dispatch.role != role
        or dispatch.agent_session != build_agent_session(project_id, role, work_item_id)
    ):
        raise OrchestrationConflict("Dispatch Project, WorkItem, role or session does not match")


def _response_for_dispatch(uow, response_id, dispatch):
    response = uow.chatgpt_responses.get(response_id)
    delivery = uow.prompt_deliveries.get(response.delivery_id) if response else None
    if delivery is None or delivery.dispatch_id != dispatch.dispatch_id:
        raise OrchestrationConflict("Response does not originate from the selected dispatch")
    return response


def _cancel_dispatch(uow, dispatch):
    if dispatch and dispatch.status == PromptDispatchStatus.PREPARED:
        dispatch.cancel()
        uow.prompt_dispatches.save(dispatch)


def _authorized_item(project, work_item_id, roadmap_reader):
    try:
        roadmap = read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError as exc:
        raise OrchestrationConflict("Canonical roadmap unavailable") from exc
    if not roadmap.pipeline.valid:
        raise OrchestrationConflict("Canonical roadmap is invalid")

    scheduler = derive_scheduler_projection(roadmap.pipeline)
    scheduler_item = next(
        (candidate for candidate in scheduler.items if candidate.work_item.key == work_item_id),
        None,
    )
    if (
        scheduler_item is None
        or not scheduler_item.executable
        or scheduler_item.work_item.type is not WorkItemType.WORK
        or scheduler_item.expected_role != PromptDispatchRole.DEV.value
    ):
        raise OrchestrationConflict(
            "WorkItem is not an authorized executable DEV target"
        )
    return roadmap, scheduler_item.work_item

def _snapshot(project, roadmap, item, source, *, predecessor=None, context_decision=None):
    return json.dumps(
        {
            "repository": project.repository_full_name,
            "roadmap": project.roadmap_issue_number,
            "pipeline_version": roadmap.pipeline.version,
            "work_item": asdict(item),
            "roadmap_updated_at": roadmap.issue.updated_at,
            "source_dispatch_id": str(source.dispatch_id),
            "source_session": source.agent_session,
            "predecessor_handoff_id": str(predecessor.handoff_id) if predecessor else None,
            "context_decision_id": str(context_decision.decision_id) if context_decision else None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _request_prompt(
    *,
    project,
    item,
    handoff_id,
    target_role,
    purpose,
    source,
    question,
    context,
    source_response_id,
    context_decision=None,
):
    role = PromptDispatchRole(target_role)
    version = "V1/V2 selon le snapshot canonique courant"
    if role is PromptDispatchRole.ARCH:
        return f"""Tu travailles sur le dépôt GitHub {project.repository_full_name} en tant qu'Architecte.
Handoff : {handoff_id}
WorkItem : {item.key} — {item.title}
Parent : {item.parent}. Roadmap maître : #{project.roadmap_issue_number} ({version}).
Session source : {source.agent_session}.
Réponse source explicitement sélectionnée : {source_response_id or 'aucune'}.
Decision de contexte : {context_decision.decision_id if context_decision else 'aucune'}.

Question confirmée :
{question}

Contexte confirmé :
{context}

Lis AGENTS.md et les ADR applicables. Consulte le main et le contexte GitHub actuels en lecture seule.
Ne modifie aucun fichier, branche, commit, PR, issue ou roadmap.
Donne une recommandation technique claire pour le WorkItem courant. Ta réponse brute reste une
ImportedChatGptResponse : elle ne devient une Decision qu'après acceptation humaine explicite.
"""
    return f"""Tu travailles sur le dépôt GitHub {project.repository_full_name} en tant que Product Owner.
Handoff : {handoff_id}
Purpose : {purpose}
WorkItem : {item.key} — {item.title}
Parent : {item.parent}. Roadmap maître : #{project.roadmap_issue_number} ({version}).
Session source : {source.agent_session}.
Réponse source explicitement sélectionnée : {source_response_id or 'aucune'}.
Decision ARCH source : {context_decision.decision_id if context_decision else 'aucune'}.
Conclusion ARCH acceptée : {context_decision.summary if context_decision else 'aucune'}.

Question confirmée :
{question}

Contexte confirmé :
{context}

Scope réellement disponible :
- WorkItem : {item.key} — {item.title}
- type : {item.type.value}
- parent : {item.parent}
- lane : {item.lane}
- aucun critère d'acceptation supplémentaire n'est présumé s'il n'apparaît pas dans le contexte ci-dessus.

Lis AGENTS.md, les ADR applicables et le contexte GitHub actuel en lecture seule.
Ne modifie aucun fichier, branche, commit, PR, issue ou roadmap.
Recommande clairement soit une clarification dans le scope courant, soit le besoin d'une décision
de scope/roadmap. Ne produis pas d'enveloppe JSON autoritaire. Ta réponse reste non autoritaire
jusqu'à la sélection et l'acceptation humaine explicites.
"""


def create_handoff(project, work_item_id, command: CreateHandoff, *, roadmap_reader, uow_factory):
    role, purpose = validate_handoff_kind(command.target_role, command.purpose)
    with uow_factory() as uow:
        existing = uow.handoffs.by_command(command.creation_command_id)
        if existing:
            expected = {
                "source_dispatch_id": command.source_dispatch_id,
                "source_response_id": command.source_response_id,
                "question": command.question,
                "context": command.context,
                "created_by": command.created_by,
                "target_role": role.value,
                "purpose": purpose.value,
            }
            if (
                (existing.project_id, existing.work_item_id) != (project.project_id, work_item_id)
                or any(getattr(existing, key) != value for key, value in expected.items())
            ):
                raise OrchestrationConflict("Creation command already used with different content")
            return existing

        roadmap, item = _authorized_item(project, work_item_id, roadmap_reader)
        source = uow.prompt_dispatches.get(command.source_dispatch_id)
        _validate_dispatch(source, project.project_id, work_item_id, PromptDispatchRole.DEV)
        if command.source_response_id:
            _response_for_dispatch(uow, command.source_response_id, source)
        if uow.handoffs.active(project.project_id, work_item_id):
            raise OrchestrationConflict("A blocking Handoff already exists for this WorkItem")

        identity = uuid4()
        snapshot = _snapshot(project, roadmap, item, source)
        prompt = _request_prompt(
            project=project,
            item=item,
            handoff_id=identity,
            target_role=role,
            purpose=purpose,
            source=source,
            question=command.question,
            context=command.context,
            source_response_id=command.source_response_id,
        )
        dispatch = create_prompt_dispatch_in_uow(
            CreatePromptDispatchCommand(
                project.project_id,
                work_item_id,
                role,
                prompt,
                f"handoff:{identity}:REQUEST:{role.value}:v1",
            ),
            uow=uow,
        )
        handoff = Handoff(
            handoff_id=identity,
            project_id=project.project_id,
            work_item_id=work_item_id,
            source_dispatch_id=source.dispatch_id,
            source_response_id=command.source_response_id,
            question=command.question,
            context=command.context,
            context_snapshot=snapshot,
            request_dispatch_id=dispatch.dispatch_id,
            creation_command_id=command.creation_command_id,
            created_by=command.created_by,
            created_at=utc_now(),
            target_role=role.value,
            purpose=purpose.value,
        )
        uow.flush()
        uow.handoffs.add(handoff)
        for pending in uow.prompt_dispatches.list_for_work_item(project.project_id, work_item_id):
            if pending.role == PromptDispatchRole.DEV:
                _cancel_dispatch(uow, pending)
        uow.commit()
        return handoff


def transfer_handoff_to_po(
    handoff_id,
    command: TransferHandoffToPO,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    _, purpose = validate_handoff_kind(PromptDispatchRole.PO, command.purpose)
    with uow_factory() as uow:
        existing = uow.handoffs.by_command(command.transfer_command_id)
        if existing:
            response_compatible = (
                existing.source_response_id == command.source_response_id
                if existing.context_decision_id is None
                else command.source_response_id in (None, existing.source_response_id)
            )
            if (
                existing.predecessor_handoff_id != handoff_id
                or existing.target_role != PromptDispatchRole.PO.value
                or existing.purpose != purpose.value
                or existing.question != command.question
                or existing.context != command.context
                or existing.created_by != command.created_by
                or not response_compatible
            ):
                raise OrchestrationConflict("Transfer command already used with different content")
            return existing

        predecessor = _require_handoff(uow, handoff_id)
        if predecessor.target_role != PromptDispatchRole.ARCH.value:
            raise OrchestrationConflict("Only an ARCH Handoff can transfer to PO")
        if predecessor.version != command.expected_version:
            raise OrchestrationConflict("Handoff version changed; refresh before confirming")

        active = uow.handoffs.active(predecessor.project_id, predecessor.work_item_id)
        if active is None or active.handoff_id != predecessor.handoff_id:
            raise OrchestrationConflict("ARCH Handoff is no longer the active blocking consultation")

        request = uow.prompt_dispatches.get(predecessor.request_dispatch_id)
        _validate_dispatch(request, predecessor.project_id, predecessor.work_item_id, PromptDispatchRole.ARCH)
        context_decision = None
        selected_response_id = command.source_response_id
        if predecessor.status is HandoffStatus.OPEN:
            if selected_response_id is None:
                raise OrchestrationConflict("OPEN ARCH transfer requires an explicitly selected response")
            _response_for_dispatch(uow, selected_response_id, request)
        elif predecessor.status is HandoffStatus.DECIDED:
            context_decision = uow.decisions.for_handoff(predecessor.handoff_id)
            if (
                context_decision is None
                or context_decision.effect is not DecisionEffect.HOLD_FOR_AUTHORIZATION
            ):
                raise OrchestrationConflict("DECIDED ARCH transfer requires a HOLD_FOR_AUTHORIZATION Decision")
            if selected_response_id not in (None, context_decision.source_response_id):
                raise OrchestrationConflict("Transfer response conflicts with the accepted ARCH Decision")
            selected_response_id = context_decision.source_response_id
        else:
            raise OrchestrationConflict("ARCH Handoff cannot be transferred from its current state")

        project = project_catalog.get(predecessor.project_id)
        if project is None:
            raise OrchestrationConflict("Project not found")
        roadmap, item = _authorized_item(project, predecessor.work_item_id, roadmap_reader)
        identity = uuid4()
        snapshot = _snapshot(
            project,
            roadmap,
            item,
            request,
            predecessor=predecessor,
            context_decision=context_decision,
        )
        prompt = _request_prompt(
            project=project,
            item=item,
            handoff_id=identity,
            target_role=PromptDispatchRole.PO,
            purpose=purpose,
            source=request,
            question=command.question,
            context=command.context,
            source_response_id=selected_response_id,
            context_decision=context_decision,
        )

        transferred = predecessor.transition(HandoffStatus.TRANSFERRED, command.expected_version)
        uow.handoffs.save(transferred, predecessor.version)
        uow.flush()

        dispatch = create_prompt_dispatch_in_uow(
            CreatePromptDispatchCommand(
                predecessor.project_id,
                predecessor.work_item_id,
                PromptDispatchRole.PO,
                prompt,
                f"handoff:{identity}:REQUEST:PO:v1",
            ),
            uow=uow,
        )
        uow.flush()
        po_handoff = Handoff(
            handoff_id=identity,
            project_id=predecessor.project_id,
            work_item_id=predecessor.work_item_id,
            source_dispatch_id=predecessor.source_dispatch_id,
            source_response_id=selected_response_id,
            question=command.question,
            context=command.context,
            context_snapshot=snapshot,
            request_dispatch_id=dispatch.dispatch_id,
            creation_command_id=command.transfer_command_id,
            created_by=command.created_by,
            created_at=utc_now(),
            target_role=PromptDispatchRole.PO.value,
            purpose=purpose.value,
            predecessor_handoff_id=predecessor.handoff_id,
            context_decision_id=context_decision.decision_id if context_decision else None,
        )
        uow.handoffs.add(po_handoff)
        _cancel_dispatch(uow, request)
        uow.commit()
        return po_handoff


def _current_resume_context(handoff, project, roadmap_reader, evidence_reader):
    projection = read_project_execution_for_work_item(
        project,
        handoff.work_item_id,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )
    allowed = (
        projection is not None
        and projection.work_item is not None
        and projection.work_item.key == handoff.work_item_id
        and projection.state
        not in (
            ExecutionState.BLOCKED,
            ExecutionState.MERGED,
            ExecutionState.ROADMAP_UPDATE_REQUIRED,
        )
    )
    return projection, allowed


def _prepare_dev_resume(
    uow,
    *,
    handoff,
    decision,
    project,
    projection,
):
    source = uow.prompt_dispatches.get(handoff.source_dispatch_id)
    _validate_dispatch(source, handoff.project_id, handoff.work_item_id, PromptDispatchRole.DEV)
    evidence_key = (
        _ci_red_idempotency_key(project, projection)
        if projection.state == ExecutionState.CI_RED
        else None
    )
    github_context = json.dumps(asdict(projection), default=str, ensure_ascii=False)
    prompt = f"""Poursuis le WorkItem existant {handoff.work_item_id} du dépôt {project.repository_full_name}.
Handoff : {handoff.handoff_id}
Question initiale : {handoff.question}
Contexte confirmé : {handoff.context}
Decision acceptée : {decision.decision_id}
Réponse source : {decision.source_response_id}
Effet confirmé : CONTINUE_IN_SCOPE
Conclusion acceptée et contraintes retenues :
{decision.summary}

Seule cette conclusion a été explicitement acceptée. La réponse brute reste historique.
Contexte GitHub actuel :
{github_context}

Respecte AGENTS.md et les ADR applicables. Revérifie le main actuel et le roadmap
#{project.roadmap_issue_number}. Poursuis uniquement {handoff.work_item_id} dans la même
session DEV {source.agent_session}. Ne commence pas la tranche suivante.
"""
    resume = create_prompt_dispatch_in_uow(
        CreatePromptDispatchCommand(
            handoff.project_id,
            handoff.work_item_id,
            PromptDispatchRole.DEV,
            prompt,
            f"decision:{decision.decision_id}:RESUME:DEV:v1",
        ),
        uow=uow,
    )
    return resume, evidence_key


def _prepare_arch_resume(uow, *, handoff, decision, project, roadmap, item):
    identity = uuid5(NAMESPACE_URL, f"devcockpit:decision:{decision.decision_id}:arch-handoff")
    command_identity = uuid5(NAMESPACE_URL, f"devcockpit:decision:{decision.decision_id}:arch-resume-command")
    prompt = f"""Reprends la consultation Architecte du WorkItem {handoff.work_item_id} du dépôt {project.repository_full_name}.
La clarification Product Owner suivante vient d'être explicitement acceptée.

PO Handoff : {handoff.handoff_id}
PO Decision : {decision.decision_id}
Conclusion acceptée :
{decision.summary}

Relis AGENTS.md, les ADR applicables et le contexte actuel en lecture seule.
Réévalue la recommandation technique à la lumière de cette clarification produit.
Ne modifie aucun fichier, branche, commit, PR, issue ou roadmap.
Ne reprends pas DEV directement : fournis une nouvelle recommandation Architecte à accepter explicitement.
"""
    dispatch = create_prompt_dispatch_in_uow(
        CreatePromptDispatchCommand(
            handoff.project_id,
            handoff.work_item_id,
            PromptDispatchRole.ARCH,
            prompt,
            f"decision:{decision.decision_id}:RESUME:ARCH:v1",
        ),
        uow=uow,
    )
    source = uow.prompt_dispatches.get(handoff.source_dispatch_id)
    snapshot = _snapshot(
        project,
        roadmap,
        item,
        dispatch,
        predecessor=handoff,
        context_decision=decision,
    )
    child = Handoff(
        handoff_id=identity,
        project_id=handoff.project_id,
        work_item_id=handoff.work_item_id,
        source_dispatch_id=handoff.source_dispatch_id,
        source_response_id=decision.source_response_id,
        question="Réévaluer l'approche technique après clarification Product Owner.",
        context=decision.summary,
        context_snapshot=snapshot,
        request_dispatch_id=dispatch.dispatch_id,
        creation_command_id=command_identity,
        created_by=decision.accepted_by,
        created_at=utc_now(),
        target_role=PromptDispatchRole.ARCH.value,
        purpose=HandoffPurpose.TECHNICAL_GUIDANCE.value,
        predecessor_handoff_id=handoff.handoff_id,
        context_decision_id=decision.decision_id,
    )
    return dispatch, child


def accept_decision(
    handoff_id,
    command: AcceptDecision,
    *,
    project_catalog,
    roadmap_reader,
    evidence_reader,
    uow_factory,
):
    with uow_factory() as uow:
        previous = uow.decisions.by_command(command.acceptance_command_id)
        if previous:
            authorization = uow.roadmap_writeback_authorizations.for_decision(
                previous.decision_id
            )
            if (
                previous.source_handoff_id != handoff_id
                or previous.source_response_id != command.source_response_id
                or previous.summary != command.summary
                or previous.effect != DecisionEffect(command.effect)
                or previous.accepted_by != command.accepted_by
                or previous.decision_type != command.decision_type
                or (authorization is not None) != command.accepts_residual_writeback_risk
            ):
                raise OrchestrationConflict("Acceptance command already used with different content")
            return _require_handoff(uow, handoff_id)

        handoff = _require_handoff(uow, handoff_id)
        target_type = validate_decision_kind(
            handoff.target_role,
            handoff.purpose,
            command.decision_type,
        )
        if command.accepts_residual_writeback_risk and not (
            handoff.target_role == PromptDispatchRole.PO.value
            and handoff.purpose == HandoffPurpose.ROADMAP_REVIEW.value
            and target_type is DecisionType.SCOPE_DECISION
        ):
            raise OrchestrationConflict(
                "Residual GitHub writeback risk can only be accepted by a PO ROADMAP_REVIEW SCOPE_DECISION"
            )
        decided = handoff.transition(HandoffStatus.DECIDED, command.expected_version)
        request = uow.prompt_dispatches.get(handoff.request_dispatch_id)
        _validate_dispatch(request, handoff.project_id, handoff.work_item_id, handoff.role)
        _response_for_dispatch(uow, command.source_response_id, request)
        decision = Decision(
            uuid4(),
            handoff_id,
            command.source_response_id,
            command.summary,
            DecisionEffect(command.effect),
            command.accepted_by,
            utc_now(),
            command.acceptance_command_id,
            target_type.value,
        )
        uow.decisions.add(decision)
        uow.flush()
        if command.accepts_residual_writeback_risk:
            uow.roadmap_writeback_authorizations.add(
                WritebackRiskAuthorization(
                    decision_id=decision.decision_id,
                    project_id=handoff.project_id,
                    accepted_by=command.accepted_by,
                    accepted_at=decision.accepted_at,
                    acceptance_command_id=command.acceptance_command_id,
                )
            )
            uow.flush()

        held_reason = "HOLD_FOR_AUTHORIZATION"
        if decision.effect is DecisionEffect.CONTINUE_IN_SCOPE:
            project = project_catalog.get(handoff.project_id)
            if project is None:
                raise OrchestrationConflict("Project not found")
            projection, allowed = _current_resume_context(
                handoff,
                project,
                roadmap_reader,
                evidence_reader,
            )
            held_reason = "Canonical WorkItem or current GitHub evidence no longer authorizes a resume"
            if allowed:
                if handoff.role is PromptDispatchRole.PO and handoff.predecessor_handoff_id is not None:
                    roadmap, item = _authorized_item(
                        project,
                        handoff.work_item_id,
                        roadmap_reader,
                    )
                    resume, child = _prepare_arch_resume(
                        uow,
                        handoff=handoff,
                        decision=decision,
                        project=project,
                        roadmap=roadmap,
                        item=item,
                    )
                    uow.flush()
                    resolved = decided.transition(
                        HandoffStatus.RESUME_PREPARED,
                        decided.version,
                        resume_dispatch_id=resume.dispatch_id,
                    )
                    uow.handoffs.save(resolved, handoff.version)
                    uow.flush()
                    uow.handoffs.add(child)
                    held_reason = None
                    decided = resolved
                else:
                    resume, evidence_key = _prepare_dev_resume(
                        uow,
                        handoff=handoff,
                        decision=decision,
                        project=project,
                        projection=projection,
                    )
                    uow.flush()
                    decided = decided.transition(
                        HandoffStatus.RESUME_PREPARED,
                        decided.version,
                        resume_dispatch_id=resume.dispatch_id,
                        covered_github_evidence=evidence_key,
                    )
                    held_reason = None

        if held_reason:
            decided = replace(decided, resume_held_reason=held_reason)
        if not (
            decision.effect is DecisionEffect.CONTINUE_IN_SCOPE
            and handoff.role is PromptDispatchRole.PO
            and handoff.predecessor_handoff_id is not None
            and held_reason is None
        ):
            uow.handoffs.save(decided, handoff.version)
        uow.commit()
        return decided


def cancel_handoff(handoff_id, command: CancelHandoff, *, uow_factory):
    with uow_factory() as uow:
        existing = uow.handoffs.by_cancellation_command(command.cancellation_command_id)
        if existing:
            if (
                existing.handoff_id != handoff_id
                or existing.cancelled_by != command.cancelled_by
                or existing.cancel_reason != command.reason
            ):
                raise OrchestrationConflict("Cancellation command already used with different content")
            return existing
        handoff = _require_handoff(uow, handoff_id)
        cancelled = handoff.transition(
            HandoffStatus.CANCELLED,
            command.expected_version,
            cancelled_by=command.cancelled_by,
            cancelled_at=utc_now(),
            cancel_reason=command.reason,
            cancellation_command_id=command.cancellation_command_id,
        )
        _cancel_dispatch(uow, uow.prompt_dispatches.get(handoff.request_dispatch_id))
        uow.handoffs.save(cancelled, handoff.version)
        uow.commit()
        return cancelled


def dispatch_payload(dispatch, uow):
    if dispatch is None:
        return None
    delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
    interaction = read_interaction_summary(dispatch, uow=uow)
    return {
        "dispatch_id": dispatch.dispatch_id,
        "agent_session": dispatch.agent_session,
        "prompt_text": dispatch.prompt_text,
        "status": dispatch.status,
        "delivery": {
            "delivery_id": delivery.delivery_id,
            "acknowledged": delivery.is_acknowledged,
        } if delivery else None,
        "interaction": asdict(interaction),
    }


def _derived_resume_role(handoff):
    if handoff.target_role == PromptDispatchRole.PO.value and handoff.predecessor_handoff_id:
        return PromptDispatchRole.ARCH.value
    return PromptDispatchRole.DEV.value


def read_orchestration(project, work_item_id, *, roadmap_reader, evidence_reader, uow_factory):
    projection = read_project_execution_for_work_item(
        project,
        work_item_id,
        roadmap_reader=roadmap_reader,
        evidence_reader=evidence_reader,
    )
    with uow_factory() as uow:
        handoffs = uow.handoffs.list_for_work_item(project.project_id, work_item_id)
        active = next((handoff for handoff in handoffs if handoff.blocking), None)
        dispatches = uow.prompt_dispatches.list_for_work_item(project.project_id, work_item_id)
        sources = [dispatch for dispatch in dispatches if dispatch.role == PromptDispatchRole.DEV]
        all_responses = uow.chatgpt_responses.list_all()

        def responses_for(dispatch_id):
            return [
                {
                    "response_id": response.response_id,
                    "delivery_id": response.delivery_id,
                    "text": response.text,
                    "imported_at": response.imported_at,
                }
                for response in all_responses
                if (delivery := uow.prompt_deliveries.get(response.delivery_id))
                and delivery.dispatch_id == dispatch_id
            ]

        consultations = []
        for handoff in handoffs:
            decision = uow.decisions.for_handoff(handoff.handoff_id)
            proposals = (
                uow.roadmap_change_proposals.list_for_decision(decision.decision_id)
                if decision else []
            )
            request_dispatch = uow.prompt_dispatches.get(handoff.request_dispatch_id)
            responses = responses_for(handoff.request_dispatch_id)
            request_interaction = read_interaction_summary(request_dispatch, uow=uow)
            transfer_allowed = (
                handoff.target_role == PromptDispatchRole.ARCH.value
                and (
                    (handoff.status is HandoffStatus.OPEN and bool(responses))
                    or (
                        handoff.status is HandoffStatus.DECIDED
                        and decision is not None
                        and decision.effect is DecisionEffect.HOLD_FOR_AUTHORIZATION
                    )
                )
            )
            proposal_allowed = (
                decision is not None
                and decision.decision_type == DecisionType.SCOPE_DECISION.value
                and decision.effect is DecisionEffect.HOLD_FOR_AUTHORIZATION
            )
            authorization = (
                uow.roadmap_writeback_authorizations.for_decision(decision.decision_id)
                if decision else None
            )
            consultations.append({
                **asdict(handoff),
                "responses": responses,
                "indication": (
                    "Réponse importée à examiner"
                    if responses
                    else interaction_indication(request_interaction)
                ) if handoff.status is HandoffStatus.OPEN else handoff.status,
                "decision": (
                    {
                        **asdict(decision),
                        "accepts_residual_writeback_risk": authorization is not None,
                    }
                    if decision else None
                ),
                "proposals": [
                    {
                        "proposal_id": proposal.proposal_id,
                        "status": proposal.status,
                        "version": proposal.version,
                        "current_revision": proposal.current_revision,
                        "confirmed_revision": proposal.confirmed_revision,
                        "confirmed_preview_digest": proposal.confirmed_preview_digest,
                        "applications": [
                            {
                                "application_id": application.application_id,
                                "status": application.status,
                                "version": application.version,
                                "revision": application.revision,
                                "last_remote_body_hash": application.last_remote_body_hash,
                            }
                            for application in uow.roadmap_change_applications.list_for_proposal(
                                proposal.proposal_id
                            )
                        ],
                    }
                    for proposal in proposals
                ],
                "request_dispatch": dispatch_payload(request_dispatch, uow),
                "resume_dispatch": (
                    dispatch_payload(uow.prompt_dispatches.get(handoff.resume_dispatch_id), uow)
                    if handoff.resume_dispatch_id else None
                ),
                "resume_role": _derived_resume_role(handoff),
                "actions": {
                    "accept": handoff.status is HandoffStatus.OPEN and bool(responses),
                    "cancel": handoff.blocking,
                    "transfer_to_po": transfer_allowed,
                    "create_proposal": proposal_allowed,
                },
            })

        authorized = (
            projection.work_item is not None
            and projection.work_item.key == work_item_id
            and projection.state != ExecutionState.BLOCKED
        )
        return {
            "work_item_id": work_item_id,
            "execution_projection": asdict(projection),
            "active_handoff_id": active.handoff_id if active else None,
            "handoffs": consultations,
            "dev_sources": [
                {
                    **dispatch_payload(dispatch, uow),
                    "responses": responses_for(dispatch.dispatch_id),
                }
                for dispatch in sources
            ],
            "allowed_handoff_kinds": [
                {"target_role": "ARCH", "purpose": "TECHNICAL_GUIDANCE"},
                {"target_role": "PO", "purpose": "PRODUCT_CLARIFICATION"},
                {"target_role": "PO", "purpose": "ROADMAP_REVIEW"},
            ],
            "actions": {
                "create_handoff": bool(authorized and sources and not active),
                "automatic_dev_inhibited": active is not None,
            },
            "transport_limitation": "Un prompt déjà accepté dans Firefox ne peut pas être révoqué à distance.",
        }
