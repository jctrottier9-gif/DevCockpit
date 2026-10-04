from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from app.application.cockpit import read_project_cockpit_overview


def build_cockpit_router(
    *,
    project_catalog,
    roadmap_reader,
    evidence_reader,
    uow_factory,
    max_parallel_dev_executions: int,
    companion_connections,
):
    router = APIRouter(tags=["cockpit"])

    @router.get("/api/projects/{project_id}/cockpit")
    def read_cockpit(project_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        projection = read_project_cockpit_overview(
            project,
            roadmap_reader=roadmap_reader,
            evidence_reader=evidence_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=max_parallel_dev_executions,
            companion_connected=companion_connections.has_active_connection,
        )
        return {
            "project": {
                "project_id": project.project_id,
                "repository_full_name": project.repository_full_name,
                "roadmap_issue_number": project.roadmap_issue_number,
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

    return router
