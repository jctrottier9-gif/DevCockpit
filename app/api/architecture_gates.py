from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.application.architecture_gates import (
    ArchitectureGateAuthorizationError,
    ArchitectureGateDispatchUnavailable,
    ArchitectureGateNotFound,
    ArchitectureGateNotReady,
    ArchitectureGateWrongType,
    authorize_architecture_gate,
)
from app.application.roadmaps import RoadmapSourceError


class AuthorizeArchitectureGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


def build_architecture_gate_router(
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
) -> APIRouter:
    router = APIRouter(tags=["architecture-gates"])

    @router.post(
        "/api/projects/{project_id}/architecture-gates/{work_item_id}/authorize"
    )
    def authorize_gate(
        project_id: str,
        work_item_id: str,
        command: AuthorizeArchitectureGateRequest,
    ) -> dict[str, object]:
        # command.confirm is deliberately required as Literal[True]. A READY gate,
        # polling, refresh, CI state or dependency satisfaction is never authority.
        _ = command.confirm
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        try:
            dispatch = authorize_architecture_gate(
                project,
                work_item_id,
                roadmap_reader=roadmap_reader,
                uow_factory=uow_factory,
            )
        except RoadmapSourceError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": exc.code,
                    "message": "Canonical roadmap source is unavailable.",
                },
            ) from exc
        except ArchitectureGateNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": exc.code, "message": "Architecture gate not found."},
            ) from exc
        except (
            ArchitectureGateWrongType,
            ArchitectureGateNotReady,
            ArchitectureGateDispatchUnavailable,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": (
                        "Architecture gate cannot be authorized from the current canonical state."
                    ),
                },
            ) from exc
        except ArchitectureGateAuthorizationError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

        return {
            "status": "AUTHORIZED",
            "project_id": project.project_id,
            "work_item_id": work_item_id,
            "dispatch_id": str(dispatch.dispatch_id),
            "agent_session": dispatch.agent_session,
        }

    return router
