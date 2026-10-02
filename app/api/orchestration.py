from dataclasses import asdict, is_dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, OperationalError

from app.application.handoffs import (
    AcceptDecision,
    CancelHandoff,
    CreateHandoff,
    TransferHandoffToPO,
    accept_decision,
    cancel_handoff,
    create_handoff,
    read_orchestration,
    transfer_handoff_to_po,
)
from app.application.roadmap_changes import (
    ApplyRoadmapChangeProposal,
    CancelRoadmapChangeProposal,
    ConfirmRoadmapChangeProposal,
    CreateRoadmapChangeProposal,
    CreateRoadmapChangeProposalRevision,
    ReconcileRoadmapChangeApplication,
    apply_roadmap_change_proposal,
    cancel_roadmap_change_proposal,
    confirm_roadmap_change_proposal,
    create_roadmap_change_proposal,
    create_roadmap_change_proposal_revision,
    preview_roadmap_change_proposal_revision,
    read_roadmap_change_proposal,
    reconcile_roadmap_change_application,
)
from app.domain.handoff import (
    DecisionEffect,
    DecisionType,
    HandoffPurpose,
    OrchestrationConflict,
)


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(CommandBody):
    creation_command_id: UUID
    source_dispatch_id: UUID
    source_response_id: UUID | None = None
    question: str = Field(min_length=1)
    context: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    target_role: Literal["ARCH", "PO"] = "ARCH"
    purpose: HandoffPurpose = HandoffPurpose.TECHNICAL_GUIDANCE


class TransferToPOBody(CommandBody):
    transfer_command_id: UUID
    expected_version: int = Field(ge=1)
    question: str = Field(min_length=1)
    context: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    purpose: HandoffPurpose = HandoffPurpose.PRODUCT_CLARIFICATION
    source_response_id: UUID | None = None


class AcceptBody(CommandBody):
    acceptance_command_id: UUID
    expected_version: int = Field(ge=1)
    source_response_id: UUID
    summary: str = Field(min_length=1)
    effect: DecisionEffect
    accepted_by: str = Field(min_length=1)
    decision_type: DecisionType = DecisionType.ARCHITECTURE_GUIDANCE


class CancelBody(CommandBody):
    cancellation_command_id: UUID
    expected_version: int = Field(ge=1)
    cancelled_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CreateProposalBody(CommandBody):
    creation_command_id: UUID
    revision_command_id: UUID
    operations: list[dict[str, Any]]
    created_by: str = Field(min_length=1)


class CreateRevisionBody(CommandBody):
    revision_command_id: UUID
    expected_version: int = Field(ge=1)
    operations: list[dict[str, Any]]
    created_by: str = Field(min_length=1)


class CancelProposalBody(CommandBody):
    cancellation_command_id: UUID
    expected_version: int = Field(ge=1)
    cancelled_by: str = Field(min_length=1)


class ConfirmProposalBody(CommandBody):
    revision: int = Field(ge=1)
    preview_digest: str = Field(min_length=1)
    expected_proposal_version: int = Field(ge=1)
    confirmation_command_id: UUID
    confirmed_by: str = Field(min_length=1)
    writeback_authorization_decision_id: UUID


class ApplyProposalBody(CommandBody):
    application_command_id: UUID
    expected_proposal_version: int = Field(ge=1)
    requested_by: str = Field(min_length=1)


class ReconcileApplicationBody(CommandBody):
    reconciliation_command_id: UUID
    expected_application_version: int = Field(ge=1)
    reconciled_by: str = Field(min_length=1)


def build_orchestration_router(
    *,
    project_catalog,
    roadmap_reader,
    roadmap_writer,
    issue_mapping_reader,
    evidence_reader,
    uow_factory,
):
    router = APIRouter(tags=["orchestration"])

    def project(identity):
        result = project_catalog.get(identity)
        if result is None:
            raise HTTPException(404, "Project not found")
        return result

    def execute(operation, *args, **kwargs):
        try:
            result = operation(*args, **kwargs, uow_factory=uow_factory)
            return asdict(result) if is_dataclass(result) else result
        except OrchestrationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(
                409,
                "Concurrent or conflicting orchestration command; refresh and retry",
            ) from exc
        except OperationalError as exc:
            raise HTTPException(
                503,
                "Persistence temporarily unavailable; retry the same command",
            ) from exc

    @router.post("/api/projects/{project_id}/work-items/{key}/handoffs")
    def create(project_id: str, key: str, body: CreateBody):
        return execute(
            create_handoff,
            project(project_id),
            key,
            CreateHandoff(**body.model_dump()),
            roadmap_reader=roadmap_reader,
        )

    @router.get("/api/projects/{project_id}/work-items/{key}/orchestration")
    def read(project_id: str, key: str):
        return read_orchestration(
            project(project_id),
            key,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
        )

    @router.post("/api/handoffs/{handoff_id}/transfer-to-po")
    def transfer_to_po(handoff_id: UUID, body: TransferToPOBody):
        return execute(
            transfer_handoff_to_po,
            handoff_id,
            TransferHandoffToPO(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
        )

    @router.post("/api/handoffs/{handoff_id}/decisions")
    def accept(handoff_id: UUID, body: AcceptBody):
        return execute(
            accept_decision,
            handoff_id,
            AcceptDecision(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
        )

    @router.post("/api/handoffs/{handoff_id}/cancel")
    def cancel(handoff_id: UUID, body: CancelBody):
        return execute(
            cancel_handoff,
            handoff_id,
            CancelHandoff(**body.model_dump()),
        )

    @router.post("/api/decisions/{decision_id}/roadmap-change-proposals")
    def create_proposal(decision_id: UUID, body: CreateProposalBody):
        return execute(
            create_roadmap_change_proposal,
            decision_id,
            CreateRoadmapChangeProposal(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
        )

    @router.post("/api/roadmap-change-proposals/{proposal_id}/revisions")
    def revise_proposal(proposal_id: UUID, body: CreateRevisionBody):
        return execute(
            create_roadmap_change_proposal_revision,
            proposal_id,
            CreateRoadmapChangeProposalRevision(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
        )

    @router.get("/api/roadmap-change-proposals/{proposal_id}")
    def get_proposal(proposal_id: UUID):
        return execute(
            read_roadmap_change_proposal,
            proposal_id,
        )

    @router.get("/api/roadmap-change-proposals/{proposal_id}/revisions/{revision}/preview")
    def preview_proposal(proposal_id: UUID, revision: int):
        if revision < 1:
            raise HTTPException(422, "revision must be >= 1")
        return execute(
            preview_roadmap_change_proposal_revision,
            proposal_id,
            revision,
        )

    @router.post("/api/roadmap-change-proposals/{proposal_id}/cancel")
    def cancel_proposal(proposal_id: UUID, body: CancelProposalBody):
        return execute(
            cancel_roadmap_change_proposal,
            proposal_id,
            CancelRoadmapChangeProposal(**body.model_dump()),
        )

    @router.post("/api/roadmap-change-proposals/{proposal_id}/confirm")
    def confirm_proposal(proposal_id: UUID, body: ConfirmProposalBody):
        return execute(
            confirm_roadmap_change_proposal,
            proposal_id,
            ConfirmRoadmapChangeProposal(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
        )

    @router.post("/api/roadmap-change-proposals/{proposal_id}/apply")
    def apply_proposal(proposal_id: UUID, body: ApplyProposalBody):
        return execute(
            apply_roadmap_change_proposal,
            proposal_id,
            ApplyRoadmapChangeProposal(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
            roadmap_writer=roadmap_writer,
            issue_mapping_reader=issue_mapping_reader,
        )

    @router.post("/api/roadmap-change-applications/{application_id}/reconcile")
    def reconcile_application(application_id: UUID, body: ReconcileApplicationBody):
        return execute(
            reconcile_roadmap_change_application,
            application_id,
            ReconcileRoadmapChangeApplication(**body.model_dump()),
            project_catalog=project_catalog,
            roadmap_reader=roadmap_reader,
        )

    return router
