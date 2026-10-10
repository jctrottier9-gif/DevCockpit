"""Explicit forward-port preparation and PR creation; no implicit cherry-pick."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.application.roadmaps import RoadmapSourceError, read_project_roadmap
from app.domain.delivery_context import (
    DeliveryContractError, DeliveryMode, validate_pair,
)
from app.domain.roadmap import WorkItemStatus
from app.domain.scheduler import derive_scheduler_projection
from app.infrastructure.github_forward_port import ForwardPortError
from app.infrastructure.github_release_workflow import ReleaseWorkflowError


class ForwardPortPrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=5)
    description: str = ""
    expected_head_sha: str = Field(min_length=40, max_length=40)


def build_forward_port_router(*, project_catalog, roadmap_reader,
                              uow_factory, workflow) -> APIRouter:
    router = APIRouter(tags=["forward-port"])

    def accepted(project_id: str, work_item_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        context = project.delivery_context_for(work_item_id)
        if context is None or context.mode is not DeliveryMode.FORWARD_PORT:
            raise HTTPException(409, "Accepted FORWARD_PORT contract required")
        source = project.delivery_context_for(context.linked_work_item)
        if source is None:
            raise HTTPException(409, "Linked source HOTFIX contract required")
        try:
            validate_pair(source, context)
            with uow_factory() as uow:
                if (uow.delivery_contexts.get(context.repository_id, work_item_id) != context
                        or uow.delivery_contexts.get(source.repository_id, source.work_item_id) != source):
                    raise HTTPException(409, "Linked delivery contracts are not accepted")
            roadmap = read_project_roadmap(project, reader=roadmap_reader)
            scheduler = derive_scheduler_projection(roadmap.pipeline)
            target = next(
                (item for item in scheduler.items if item.work_item.key == work_item_id),
                None,
            )
            source_item = next(
                (item for item in scheduler.items
                 if item.work_item.key == source.work_item_id), None,
            )
            if (not scheduler.valid or target is None or not target.executable
                    or source_item is None
                    or source_item.work_item.status is not WorkItemStatus.DONE
                    or source.work_item_id not in target.dependencies):
                raise HTTPException(409, "Source hotfix must be DONE and forward-port READY")
        except (DeliveryContractError, RoadmapSourceError, ValueError) as exc:
            raise HTTPException(409, "Accepted forward-port roadmap/contract unavailable") from exc
        return project, context, source

    @router.post("/api/projects/{project_id}/deliveries/{work_item_id}/forward-port/prepare")
    def prepare(project_id: str, work_item_id: str):
        project, context, source = accepted(project_id, work_item_id)
        try:
            proof = workflow.prepare_forward_port(project, context, source)
            return {
                "work_item": context.work_item_id,
                "branch": proof.branch,
                "main_sha": proof.main_sha,
                "head_sha": proof.head_sha,
                "source_hotfix_pr": proof.source_pr,
                "integrated_commits": proof.source_commits,
                "method": proof.method,
                "action": proof.action,
                "delivery_fingerprint": context.fingerprint(),
                "instruction": (
                    "DEV: apply only the proven integrated fix with git cherry-pick -x "
                    "or explicitly reviewed conflict/merge-delta adaptation. "
                    "Do not merge the release branch into main."
                ),
            }
        except (ForwardPortError, ReleaseWorkflowError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/projects/{project_id}/deliveries/{work_item_id}/forward-port/pr")
    def open_pr(project_id: str, work_item_id: str, body: ForwardPortPrRequest):
        project, context, source = accepted(project_id, work_item_id)
        try:
            pr = workflow.ensure_forward_port_pr(
                project, context, source, title=body.title,
                description=body.description, expected_head_sha=body.expected_head_sha,
            )
            return {"number": pr.number, "base": "main", "head_sha": pr.head_sha,
                    "url": pr.url, "created": pr.created}
        except (ForwardPortError, ReleaseWorkflowError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
