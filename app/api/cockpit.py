from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.application.cockpit import read_project_cockpit_overview
from app.application.roadmap_explorer import (
    RoadmapExplorerIssueError,
    RoadmapExplorerIssueNotFoundError,
    RoadmapExplorerIssueNotReferencedError,
    read_project_roadmap_explorer,
    read_project_roadmap_issue_detail,
)
from app.application.roadmaps import RoadmapSourceError


def build_cockpit_router(
    *,
    project_catalog,
    roadmap_reader,
    evidence_reader,
    issue_reader,
    uow_factory,
    max_parallel_dev_executions: int,
    dev_stale_after_seconds: float,
    companion_connections,
):
    router = APIRouter(tags=["cockpit"])

    def project(project_id: str):
        result = project_catalog.get(project_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return result

    @router.get("/api/projects/{project_id}/cockpit")
    def read_cockpit(project_id: str):
        active_project = project(project_id)

        projection = read_project_cockpit_overview(
            active_project,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=max_parallel_dev_executions,
            companion_connected=companion_connections.has_active_connection,
            dev_stale_after_seconds=dev_stale_after_seconds,
        )
        return {
            "project": {
                "project_id": active_project.project_id,
                "repository_full_name": active_project.repository_full_name,
                "roadmap_issue_number": active_project.roadmap_issue_number,
            },
            "observed_at": projection.observed_at.isoformat(),
            "sources": {
                "roadmap": asdict(projection.roadmap_source),
                "executions": asdict(projection.execution_source),
                "attention": asdict(projection.attention_source),
            },
            "attention": asdict(projection.attention),
            "roles": [asdict(role) for role in projection.roles],
            "horizons": {
                "now": asdict(projection.now) if projection.now is not None else None,
                "parallel": [asdict(item) for item in projection.parallel],
                "next": asdict(projection.next) if projection.next is not None else None,
            },
            "dev_pool": asdict(projection.dev_pool),
        }

    @router.get("/api/projects/{project_id}/roadmap-explorer")
    def read_roadmap_explorer(project_id: str):
        active_project = project(project_id)
        try:
            projection = read_project_roadmap_explorer(
                active_project,
                roadmap_reader=roadmap_reader,
            )
        except RoadmapSourceError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "project": {
                        "project_id": active_project.project_id,
                        "repository_full_name": active_project.repository_full_name,
                        "roadmap_issue_number": active_project.roadmap_issue_number,
                    },
                    "source": {"status": "unavailable", "code": exc.code},
                },
            )

        return {
            "project": {
                "project_id": active_project.project_id,
                "repository_full_name": active_project.repository_full_name,
                "roadmap_issue_number": active_project.roadmap_issue_number,
            },
            "observed_at": projection.observed_at.isoformat(),
            "source": {
                "status": "available",
                "issue_number": projection.roadmap_issue.number,
                "url": projection.roadmap_issue.url,
                "updated_at": projection.roadmap_updated_at,
                "revision": projection.revision,
            },
            "pipeline": {
                "valid": projection.pipeline_valid,
                "version": projection.pipeline_version,
                "diagnostics": [asdict(item) for item in projection.pipeline_diagnostics],
            },
            "scheduler": {
                "valid": projection.scheduler_valid,
                "diagnostics": [asdict(item) for item in projection.scheduler_diagnostics],
            },
            "issue_mapping_diagnostics": [
                asdict(item) for item in projection.issue_mapping_diagnostics
            ],
            "horizons": {
                "now": projection.now,
                "parallel": list(projection.parallel),
                "next": projection.next,
                "later": list(projection.later),
                "history": list(projection.history),
            },
            "items": [asdict(item) for item in projection.items],
        }

    @router.get("/api/projects/{project_id}/roadmap-explorer/issues/{issue_number}")
    def read_roadmap_issue(project_id: str, issue_number: int):
        active_project = project(project_id)
        try:
            detail = read_project_roadmap_issue_detail(
                active_project,
                issue_number,
                roadmap_reader=roadmap_reader,
                issue_reader=issue_reader,
            )
        except RoadmapExplorerIssueNotReferencedError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RoadmapExplorerIssueNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RoadmapExplorerIssueError, RoadmapSourceError) as exc:
            code = getattr(exc, "code", "GITHUB_UNAVAILABLE")
            raise HTTPException(
                status_code=502,
                detail={"code": code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(detail)

    return router
