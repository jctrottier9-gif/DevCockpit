from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from app.application.roadmaps import RoadmapSourceError, read_project_roadmap
from app.domain.handoff import DecisionEffect, DecisionType, OrchestrationConflict, utc_now
from app.domain.roadmap_change import (
    GENERATOR_VERSION,
    VALIDATION_VERSION,
    ProposalStatus,
    RoadmapChangeProposal,
    RoadmapChangeProposalRevision,
    body_hash,
    build_preview,
    canonical_operations,
    generate_proposed_body,
    operations_json,
)


@dataclass(frozen=True)
class CreateRoadmapChangeProposal:
    creation_command_id: UUID
    revision_command_id: UUID
    operations: Any
    created_by: str


@dataclass(frozen=True)
class CreateRoadmapChangeProposalRevision:
    revision_command_id: UUID
    expected_version: int
    operations: Any
    created_by: str


@dataclass(frozen=True)
class CancelRoadmapChangeProposal:
    cancellation_command_id: UUID
    expected_version: int
    cancelled_by: str


def _require_proposal(uow, identity):
    proposal = uow.roadmap_change_proposals.get(identity)
    if proposal is None:
        raise OrchestrationConflict("RoadmapChangeProposal not found")
    return proposal


def _require_admissible_decision(uow, decision_id):
    decision = uow.decisions.get(decision_id)
    if decision is None:
        raise OrchestrationConflict("Decision not found")
    if (
        decision.decision_type != DecisionType.SCOPE_DECISION.value
        or decision.effect is not DecisionEffect.HOLD_FOR_AUTHORIZATION
    ):
        raise OrchestrationConflict(
            "RoadmapChangeProposal requires an accepted SCOPE_DECISION held for authorization"
        )
    handoff = uow.handoffs.get(decision.source_handoff_id)
    if handoff is None or handoff.target_role != "PO":
        raise OrchestrationConflict("RoadmapChangeProposal requires Product Owner Decision provenance")
    return decision, handoff


def _read_target(project, roadmap_reader):
    try:
        return read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError as exc:
        raise OrchestrationConflict("Canonical roadmap unavailable") from exc


def _new_revision(
    proposal,
    revision_number,
    command_id,
    operations,
    created_by,
    roadmap,
):
    normalized = canonical_operations(operations)
    proposed_body = generate_proposed_body(roadmap.issue.body, normalized)
    return RoadmapChangeProposalRevision(
        proposal_id=proposal.proposal_id,
        revision=revision_number,
        base_body=roadmap.issue.body,
        base_body_hash=body_hash(roadmap.issue.body),
        base_updated_at=roadmap.issue.updated_at,
        proposed_body=proposed_body,
        proposed_body_hash=body_hash(proposed_body),
        operations_json=operations_json(normalized),
        generator_version=GENERATOR_VERSION,
        validation_version=VALIDATION_VERSION,
        created_by=created_by,
        created_at=utc_now(),
        revision_command_id=command_id,
    )


def create_roadmap_change_proposal(
    decision_id,
    command: CreateRoadmapChangeProposal,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    if not command.created_by.strip():
        raise ValueError("created_by must not be blank")
    normalized = canonical_operations(command.operations)
    with uow_factory() as uow:
        existing = uow.roadmap_change_proposals.by_command(command.creation_command_id)
        if existing:
            revision = uow.roadmap_change_proposal_revisions.get(existing.proposal_id, 1)
            if (
                existing.source_decision_id != decision_id
                or existing.created_by != command.created_by
                or revision is None
                or revision.revision_command_id != command.revision_command_id
                or revision.operations != normalized
            ):
                raise OrchestrationConflict("Proposal creation command already used with different content")
            return existing

        _, handoff = _require_admissible_decision(uow, decision_id)
        project = project_catalog.get(handoff.project_id)
        if project is None:
            raise OrchestrationConflict("Project not found")
        roadmap = _read_target(project, roadmap_reader)

        proposal = RoadmapChangeProposal(
            proposal_id=uuid4(),
            project_id=project.project_id,
            repository_full_name=project.repository_full_name,
            roadmap_issue_number=project.roadmap_issue_number,
            source_decision_id=decision_id,
            status=ProposalStatus.DRAFT,
            version=1,
            current_revision=1,
            creation_command_id=command.creation_command_id,
            created_by=command.created_by,
            created_at=utc_now(),
        )
        revision = _new_revision(
            proposal,
            1,
            command.revision_command_id,
            normalized,
            command.created_by,
            roadmap,
        )
        uow.roadmap_change_proposals.add(proposal)
        uow.flush()
        uow.roadmap_change_proposal_revisions.add(revision)
        uow.commit()
        return proposal


def create_roadmap_change_proposal_revision(
    proposal_id,
    command: CreateRoadmapChangeProposalRevision,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    if not command.created_by.strip():
        raise ValueError("created_by must not be blank")
    normalized = canonical_operations(command.operations)
    with uow_factory() as uow:
        replay = uow.roadmap_change_proposal_revisions.by_command(command.revision_command_id)
        if replay:
            if (
                replay.proposal_id != proposal_id
                or replay.created_by != command.created_by
                or replay.operations != normalized
            ):
                raise OrchestrationConflict("Revision command already used with different content")
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Cancelled proposal cannot be revised")
        if proposal.version != command.expected_version:
            raise OrchestrationConflict("Proposal version changed; refresh before revising")

        project = project_catalog.get(proposal.project_id)
        if project is None:
            raise OrchestrationConflict("Project not found")
        if (
            project.repository_full_name != proposal.repository_full_name
            or project.roadmap_issue_number != proposal.roadmap_issue_number
        ):
            raise OrchestrationConflict("Proposal GitHub target is frozen and no longer matches project configuration")

        roadmap = _read_target(project, roadmap_reader)
        next_revision = proposal.current_revision + 1
        revision = _new_revision(
            proposal,
            next_revision,
            command.revision_command_id,
            normalized,
            command.created_by,
            roadmap,
        )
        updated = replace(
            proposal,
            version=proposal.version + 1,
            current_revision=next_revision,
        )
        uow.roadmap_change_proposal_revisions.add(revision)
        uow.roadmap_change_proposals.save(updated, command.expected_version)
        uow.commit()
        return revision


def cancel_roadmap_change_proposal(
    proposal_id,
    command: CancelRoadmapChangeProposal,
    *,
    uow_factory,
):
    if not command.cancelled_by.strip():
        raise ValueError("cancelled_by must not be blank")
    with uow_factory() as uow:
        replay = uow.roadmap_change_proposals.by_cancellation_command(
            command.cancellation_command_id
        )
        if replay:
            if replay.proposal_id != proposal_id or replay.cancelled_by != command.cancelled_by:
                raise OrchestrationConflict("Cancellation command already used with different content")
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Proposal is not cancellable")
        if proposal.version != command.expected_version:
            raise OrchestrationConflict("Proposal version changed; refresh before cancelling")
        cancelled = replace(
            proposal,
            status=ProposalStatus.CANCELLED,
            version=proposal.version + 1,
            cancelled_by=command.cancelled_by,
            cancelled_at=utc_now(),
            cancellation_command_id=command.cancellation_command_id,
        )
        uow.roadmap_change_proposals.save(cancelled, command.expected_version)
        uow.commit()
        return cancelled


def read_roadmap_change_proposal(proposal_id, *, uow_factory):
    with uow_factory() as uow:
        proposal = _require_proposal(uow, proposal_id)
        revisions = uow.roadmap_change_proposal_revisions.list_for_proposal(proposal_id)
        return {
            **asdict(proposal),
            "revisions": [
                {
                    "revision": revision.revision,
                    "base_body_hash": revision.base_body_hash,
                    "base_updated_at": revision.base_updated_at,
                    "proposed_body_hash": revision.proposed_body_hash,
                    "operations": list(revision.operations),
                    "generator_version": revision.generator_version,
                    "validation_version": revision.validation_version,
                    "created_by": revision.created_by,
                    "created_at": revision.created_at,
                    "revision_command_id": revision.revision_command_id,
                }
                for revision in revisions
            ],
            "allowed_actions": (
                ["CREATE_REVISION", "PREVIEW", "CANCEL"]
                if proposal.status is ProposalStatus.DRAFT else ["PREVIEW"]
            ),
            "github_writeback_available": False,
            "github_writeback_note": "Application GitHub disponible dans DC-041B.",
        }


def preview_roadmap_change_proposal_revision(
    proposal_id,
    revision_number,
    *,
    uow_factory,
):
    with uow_factory() as uow:
        proposal = _require_proposal(uow, proposal_id)
        revision = uow.roadmap_change_proposal_revisions.get(proposal_id, revision_number)
        if revision is None:
            raise OrchestrationConflict("Proposal revision not found")
        return build_preview(proposal, revision)
