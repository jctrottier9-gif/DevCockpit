"""Explicit user commands. Imported prose is never interpreted as a command."""
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4
import json

from app.application.executions import read_project_execution, _ci_red_idempotency_key
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch_in_uow
from app.application.roadmaps import read_project_roadmap, RoadmapSourceError
from app.domain.execution import ExecutionState
from app.domain.handoff import (Handoff, HandoffStatus, Decision, DecisionEffect,
                                OrchestrationConflict, utc_now)
from app.domain.prompt_dispatch import PromptDispatchRole, PromptDispatchStatus, build_agent_session
from app.domain.roadmap import WorkItemType


@dataclass(frozen=True)
class CreateHandoff:
    creation_command_id: UUID
    source_dispatch_id: UUID
    question: str
    context: str
    created_by: str
    source_response_id: UUID | None = None


@dataclass(frozen=True)
class AcceptDecision:
    acceptance_command_id: UUID
    expected_version: int
    source_response_id: UUID
    summary: str
    effect: DecisionEffect
    accepted_by: str


@dataclass(frozen=True)
class CancelHandoff:
    cancellation_command_id: UUID
    expected_version: int
    cancelled_by: str
    reason: str


def _require_handoff(uow, identity):
    handoff = uow.handoffs.get(identity)
    if handoff is None:
        raise OrchestrationConflict('Handoff not found')
    return handoff


def _validate_dispatch(dispatch, project_id, work_item_id, role):
    if (dispatch is None or dispatch.project_id != project_id
            or dispatch.work_item_id != work_item_id or dispatch.role != role
            or dispatch.agent_session != build_agent_session(project_id, role, work_item_id)):
        raise OrchestrationConflict('Dispatch Project, WorkItem, role or session does not match')


def _response_for_dispatch(uow, response_id, dispatch):
    response = uow.chatgpt_responses.get(response_id)
    delivery = uow.prompt_deliveries.get(response.delivery_id) if response else None
    if delivery is None or delivery.dispatch_id != dispatch.dispatch_id:
        raise OrchestrationConflict('Response does not originate from the selected dispatch')
    return response


def _cancel_dispatch(uow, dispatch):
    if dispatch and dispatch.status == PromptDispatchStatus.PREPARED:
        dispatch.cancel()
        uow.prompt_dispatches.save(dispatch)


def create_handoff(project, work_item_id, command: CreateHandoff, *, roadmap_reader, uow_factory):
    with uow_factory() as uow:
        existing = uow.handoffs.by_command(command.creation_command_id)
        if existing:
            matches = all(getattr(existing, k) == v for k, v in asdict(command).items())
            if not matches or (existing.project_id, existing.work_item_id) != (project.project_id, work_item_id):
                raise OrchestrationConflict('Creation command already used with different content')
            return existing
        try:
            roadmap = read_project_roadmap(project, reader=roadmap_reader)
        except RoadmapSourceError as exc:
            raise OrchestrationConflict('Canonical roadmap unavailable') from exc
        item = roadmap.pipeline.active_ready_item
        if not roadmap.pipeline.valid or item is None or item.key != work_item_id or item.type != WorkItemType.WORK:
            raise OrchestrationConflict('WorkItem is not the authorized MAIN work')
        source = uow.prompt_dispatches.get(command.source_dispatch_id)
        _validate_dispatch(source, project.project_id, work_item_id, PromptDispatchRole.DEV)
        if command.source_response_id:
            _response_for_dispatch(uow, command.source_response_id, source)
        if uow.handoffs.active(project.project_id, work_item_id):
            raise OrchestrationConflict('A blocking Handoff already exists for this WorkItem')
        identity = uuid4()
        snapshot = json.dumps({
            'repository': project.repository_full_name, 'roadmap': project.roadmap_issue_number,
            'work_item': asdict(item), 'roadmap_updated_at': roadmap.issue.updated_at,
            'source_dispatch_id': str(source.dispatch_id), 'source_session': source.agent_session,
        }, ensure_ascii=False)
        prompt = f'''Tu travailles sur le dépôt GitHub `{project.repository_full_name}` en tant qu'Architecte.
Handoff : {identity}
WorkItem : {item.key} — {item.title}
Parent : {item.parent}. Roadmap maître : #{project.roadmap_issue_number} / COCKPIT_PIPELINE_V1.
Source DEV : {source.dispatch_id}, session {source.agent_session}.
Réponse DEV source : {command.source_response_id or 'non sélectionnée'}.

Question confirmée :
{command.question}

Contexte confirmé :
{command.context}

Lis AGENTS.md et les ADR applicables, revérifie le main actuel et le roadmap GitHub.
Analyse en lecture seule, strictement pour ce WorkItem. Aucune modification de fichier,
aucune branche, aucun commit, aucune PR, aucune mutation du roadmap.
Propose une conclusion et les contraintes pertinentes. Ta recommandation brute n'est pas
une Decision acceptée ni une autorisation de modifier le scope ou les ADR.
'''
        dispatch = create_prompt_dispatch_in_uow(CreatePromptDispatchCommand(
            project.project_id, work_item_id, 'ARCH', prompt,
            f'handoff:{identity}:REQUEST:ARCH:v1'), uow=uow)
        handoff = Handoff(handoff_id=identity, project_id=project.project_id,
            work_item_id=work_item_id, source_dispatch_id=source.dispatch_id,
            source_response_id=command.source_response_id, question=command.question,
            context=command.context, context_snapshot=snapshot,
            request_dispatch_id=dispatch.dispatch_id, creation_command_id=command.creation_command_id,
            created_by=command.created_by, created_at=utc_now())
        uow.flush()  # Request FK must exist before inserting the Handoff.
        uow.handoffs.add(handoff)
        for pending in uow.prompt_dispatches.list_for_work_item(project.project_id, work_item_id):
            if pending.role == PromptDispatchRole.DEV:
                _cancel_dispatch(uow, pending)
        uow.commit()
        return handoff


def accept_decision(handoff_id, command: AcceptDecision, *, project_catalog,
                    roadmap_reader, evidence_reader, uow_factory):
    with uow_factory() as uow:
        previous = uow.decisions.by_command(command.acceptance_command_id)
        if previous:
            if (previous.source_handoff_id != handoff_id or
                    any(getattr(previous, key) != value for key, value in asdict(command).items()
                        if key != 'expected_version')):
                raise OrchestrationConflict('Acceptance command already used with different content')
            return _require_handoff(uow, handoff_id)
        handoff = _require_handoff(uow, handoff_id)
        # Transition validates both OPEN and the optimistic client version.
        decided = handoff.transition(HandoffStatus.DECIDED, command.expected_version)
        request = uow.prompt_dispatches.get(handoff.request_dispatch_id)
        _validate_dispatch(request, handoff.project_id, handoff.work_item_id, PromptDispatchRole.ARCH)
        _response_for_dispatch(uow, command.source_response_id, request)
        decision = Decision(uuid4(), handoff_id, command.source_response_id, command.summary,
                            DecisionEffect(command.effect), command.accepted_by, utc_now(),
                            command.acceptance_command_id)
        uow.decisions.add(decision)
        held_reason = 'HOLD_FOR_AUTHORIZATION'
        if decision.effect == DecisionEffect.CONTINUE_IN_SCOPE:
            project = project_catalog.get(handoff.project_id)
            projection = read_project_execution(project, roadmap_reader=roadmap_reader,
                evidence_reader=evidence_reader) if project else None
            allowed = (projection is not None and projection.work_item is not None
                and projection.work_item.key == handoff.work_item_id
                and projection.state not in (ExecutionState.BLOCKED, ExecutionState.MERGED,
                                             ExecutionState.ROADMAP_UPDATE_REQUIRED))
            held_reason = 'Canonical WorkItem or current GitHub evidence no longer authorizes a resume'
            if allowed:
                source = uow.prompt_dispatches.get(handoff.source_dispatch_id)
                _validate_dispatch(source, handoff.project_id, handoff.work_item_id, PromptDispatchRole.DEV)
                evidence_key = (_ci_red_idempotency_key(project, projection)
                                if projection.state == ExecutionState.CI_RED else None)
                github_context = json.dumps(asdict(projection), default=str, ensure_ascii=False)
                prompt = f'''Poursuis le WorkItem existant {handoff.work_item_id} du dépôt `{project.repository_full_name}`.
Handoff : {handoff.handoff_id}
Question initiale : {handoff.question}
Contexte confirmé : {handoff.context}
Decision acceptée : {decision.decision_id}
Réponse ARCH source : {decision.source_response_id}
Effet confirmé : CONTINUE_IN_SCOPE
Conclusion acceptée et contraintes retenues :
{decision.summary}

Seule cette conclusion a été explicitement acceptée. La réponse brute Architecte reste
une recommandation historique et n'est pas une autorisation supplémentaire.
Contexte GitHub actuel (y compris les cycles CI couverts) :
{github_context}

Respecte AGENTS.md et les ADR applicables. Revérifie le main actuel et le roadmap
#{project.roadmap_issue_number}. Poursuis uniquement {handoff.work_item_id} dans la même
session DEV {source.agent_session}. Ne commence pas la tranche suivante.
'''
                resume = create_prompt_dispatch_in_uow(CreatePromptDispatchCommand(
                    handoff.project_id, handoff.work_item_id, 'DEV', prompt,
                    f'decision:{decision.decision_id}:RESUME:DEV:v1'), uow=uow)
                uow.flush()
                decided = decided.transition(HandoffStatus.RESUME_PREPARED, decided.version,
                    resume_dispatch_id=resume.dispatch_id, covered_github_evidence=evidence_key)
                held_reason = None
        if held_reason:
            from dataclasses import replace
            decided = replace(decided, resume_held_reason=held_reason)
        uow.handoffs.save(decided, handoff.version)
        uow.commit()
        return decided


def cancel_handoff(handoff_id, command: CancelHandoff, *, uow_factory):
    with uow_factory() as uow:
        existing = uow.handoffs.by_cancellation_command(command.cancellation_command_id)
        if existing:
            if (existing.handoff_id != handoff_id or existing.cancelled_by != command.cancelled_by
                    or existing.cancel_reason != command.reason):
                raise OrchestrationConflict('Cancellation command already used with different content')
            return existing
        handoff = _require_handoff(uow, handoff_id)
        cancelled = handoff.transition(HandoffStatus.CANCELLED, command.expected_version,
            cancelled_by=command.cancelled_by, cancelled_at=utc_now(), cancel_reason=command.reason,
            cancellation_command_id=command.cancellation_command_id)
        _cancel_dispatch(uow, uow.prompt_dispatches.get(handoff.request_dispatch_id))
        uow.handoffs.save(cancelled, handoff.version)
        uow.commit()
        return cancelled


def dispatch_payload(dispatch, uow):
    if dispatch is None:
        return None
    delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
    return {'dispatch_id': dispatch.dispatch_id, 'agent_session': dispatch.agent_session,
            'prompt_text': dispatch.prompt_text, 'status': dispatch.status,
            'delivery': {'delivery_id': delivery.delivery_id, 'acknowledged': delivery.is_acknowledged} if delivery else None}


def read_orchestration(project, work_item_id, *, roadmap_reader, evidence_reader, uow_factory):
    projection = read_project_execution(project, roadmap_reader=roadmap_reader, evidence_reader=evidence_reader)
    with uow_factory() as uow:
        handoffs = uow.handoffs.list_for_work_item(project.project_id, work_item_id)
        active = next((h for h in handoffs if h.blocking), None)
        dispatches = uow.prompt_dispatches.list_for_work_item(project.project_id, work_item_id)
        sources = [d for d in dispatches if d.role == PromptDispatchRole.DEV]
        all_responses = uow.chatgpt_responses.list_all()
        def responses_for(dispatch_id):
            return [{'response_id': r.response_id, 'delivery_id': r.delivery_id, 'text': r.text, 'imported_at': r.imported_at} for r in all_responses
                    if (delivery := uow.prompt_deliveries.get(r.delivery_id))
                    and delivery.dispatch_id == dispatch_id]
        consultations = []
        for h in handoffs:
            decision = uow.decisions.for_handoff(h.handoff_id)
            responses = responses_for(h.request_dispatch_id)
            consultations.append({
                **asdict(h), 'responses': responses,
                'indication': ('Réponse à examiner' if responses else 'En attente de réponse') if h.status == HandoffStatus.OPEN else h.status,
                'decision': asdict(decision) if decision else None,
                'request_dispatch': dispatch_payload(uow.prompt_dispatches.get(h.request_dispatch_id), uow),
                'resume_dispatch': dispatch_payload(uow.prompt_dispatches.get(h.resume_dispatch_id), uow) if h.resume_dispatch_id else None,
                'actions': {'accept': h.status == HandoffStatus.OPEN and bool(responses), 'cancel': h.blocking},
            })
        authorized = (projection.work_item is not None and projection.work_item.key == work_item_id
                      and projection.state != ExecutionState.BLOCKED)
        return {'work_item_id': work_item_id, 'execution_projection': asdict(projection),
                'active_handoff_id': active.handoff_id if active else None,
                'handoffs': consultations,
                'dev_sources': [{**dispatch_payload(d, uow), 'responses': responses_for(d.dispatch_id)} for d in sources],
                'actions': {'create_handoff': bool(authorized and sources and not active),
                            'automatic_dev_inhibited': active is not None},
                'transport_limitation': 'Un prompt déjà accepté dans Firefox ne peut pas être révoqué à distance.'}
