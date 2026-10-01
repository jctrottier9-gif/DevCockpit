from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, OperationalError

from app.application.handoffs import (CreateHandoff, AcceptDecision, CancelHandoff,
    create_handoff, accept_decision, cancel_handoff, read_orchestration)
from app.domain.handoff import DecisionEffect, OrchestrationConflict


class CommandBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CreateBody(CommandBody):
    creation_command_id: UUID
    source_dispatch_id: UUID
    source_response_id: UUID | None = None
    question: str = Field(min_length=1)
    context: str = Field(min_length=1)
    created_by: str = Field(min_length=1)


class AcceptBody(CommandBody):
    acceptance_command_id: UUID
    expected_version: int = Field(ge=1)
    source_response_id: UUID
    summary: str = Field(min_length=1)
    effect: DecisionEffect
    accepted_by: str = Field(min_length=1)


class CancelBody(CommandBody):
    cancellation_command_id: UUID
    expected_version: int = Field(ge=1)
    cancelled_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def build_orchestration_router(*, project_catalog, roadmap_reader, evidence_reader, uow_factory):
    router = APIRouter(tags=['orchestration'])

    def project(identity):
        result = project_catalog.get(identity)
        if result is None:
            raise HTTPException(404, 'Project not found')
        return result

    def mutate(operation, *args, **kwargs):
        try:
            return asdict(operation(*args, **kwargs, uow_factory=uow_factory))
        except OrchestrationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(409, 'Concurrent or conflicting orchestration command; refresh and retry') from exc
        except OperationalError as exc:
            raise HTTPException(503, 'Persistence temporarily unavailable; retry the same command') from exc

    @router.post('/api/projects/{project_id}/work-items/{key}/handoffs')
    def create(project_id: str, key: str, body: CreateBody):
        return mutate(create_handoff, project(project_id), key,
                      CreateHandoff(**body.model_dump()), roadmap_reader=roadmap_reader)

    @router.get('/api/projects/{project_id}/work-items/{key}/orchestration')
    def read(project_id: str, key: str):
        return read_orchestration(project(project_id), key, roadmap_reader=roadmap_reader,
                                  evidence_reader=evidence_reader, uow_factory=uow_factory)

    @router.post('/api/handoffs/{handoff_id}/decisions')
    def accept(handoff_id: UUID, body: AcceptBody):
        return mutate(accept_decision, handoff_id, AcceptDecision(**body.model_dump()),
            project_catalog=project_catalog, roadmap_reader=roadmap_reader, evidence_reader=evidence_reader)

    @router.post('/api/handoffs/{handoff_id}/cancel')
    def cancel(handoff_id: UUID, body: CancelBody):
        return mutate(cancel_handoff, handoff_id, CancelHandoff(**body.model_dump()))

    return router
