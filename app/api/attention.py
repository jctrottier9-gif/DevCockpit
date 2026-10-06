from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from app.application.attention import read_project_attention
from app.application.roadmaps import RoadmapSourceError


def build_attention_router(
    *,
    project_catalog,
    roadmap_reader,
    evidence_reader,
    uow_factory,
    max_parallel_dev_executions: int,
    companion_connections,
    dev_stale_after_seconds: float = 3600.0,
    pr_no_ci_after_seconds: float = 900.0,
    ci_stall_after_seconds: float = 1800.0,
    auto_merge_grace_seconds: float = 600.0,
):
    router = APIRouter(tags=["attention"])

    @router.get("/api/projects/{project_id}/attention")
    def read_attention(project_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            projection = read_project_attention(
                project,
                roadmap_reader=roadmap_reader,
                evidence_reader=evidence_reader,
                uow_factory=uow_factory,
                max_parallel_dev_executions=max_parallel_dev_executions,
                companion_connected=companion_connections.has_active_connection,
                dev_stale_after_seconds=dev_stale_after_seconds,
                pr_no_ci_after_seconds=pr_no_ci_after_seconds,
                ci_stall_after_seconds=ci_stall_after_seconds,
                auto_merge_grace_seconds=auto_merge_grace_seconds,
            )
        except RoadmapSourceError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": exc.code,
                    "message": "Attention projection unavailable because the canonical roadmap cannot be read.",
                },
            ) from exc
        return {
            "state": projection.state.value,
            "counts": {
                "action": projection.action_count,
                "watch": projection.watch_count,
            },
            "items": [asdict(item) for item in projection.items],
        }

    return router
