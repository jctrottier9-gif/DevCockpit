"""Read-only maintained-release supervision endpoint."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.application.release_panel import read_project_release_panel
from app.application.roadmaps import RoadmapSourceError


def build_release_panel_router(*, project_catalog, roadmap_reader, evidence_reader,
                               artifact_reader, uow_factory) -> APIRouter:
    router = APIRouter(tags=["release"])

    @router.get("/api/projects/{project_id}/release-panel")
    def release_panel(project_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        try:
            projection = read_project_release_panel(
                project, roadmap_reader=roadmap_reader, evidence_reader=evidence_reader,
                artifact_reader=artifact_reader, uow_factory=uow_factory,
            )
        except RoadmapSourceError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "project": {"project_id": project.project_id,
                                "repository_full_name": project.repository_full_name},
                    "source": {"status": "unavailable", "code": exc.code},
                    "items": [], "diagnostics": [exc.code],
                },
            )
        return projection

    return router
