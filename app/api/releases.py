"""Explicit, accepted release/hotfix operations. No implicit deployment."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.application.roadmaps import RoadmapSourceError, read_project_roadmap
from app.domain.delivery_context import DeliveryContractError, DeliveryMode
from app.domain.scheduler import derive_scheduler_projection
from app.infrastructure.github_release_workflow import ReleaseWorkflowError


class HotfixPrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=5)
    description: str = ""
    expected_head_sha: str = Field(min_length=40, max_length=40)


def build_release_router(*, project_catalog, roadmap_reader,
                         uow_factory, workflow) -> APIRouter:
    router = APIRouter(tags=["release"])

    def accepted(project_id: str, work_item_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        context = project.delivery_context_for(work_item_id)
        if context is None or context.mode not in {DeliveryMode.RELEASE, DeliveryMode.HOTFIX}:
            raise HTTPException(409, "Accepted RELEASE/HOTFIX contract required")
        try:
            with uow_factory() as uow:
                persisted = uow.delivery_contexts.get(context.repository_id, work_item_id)
            if persisted != context:
                raise HTTPException(409, "Delivery context has no matching persisted acceptance")
            roadmap = read_project_roadmap(project, reader=roadmap_reader)
            scheduler = derive_scheduler_projection(roadmap.pipeline)
            candidate = next(
                (item for item in scheduler.items if item.work_item.key == work_item_id), None
            )
            if not scheduler.valid or candidate is None or not candidate.executable:
                raise HTTPException(409, "WorkItem is not authorized by the current canonical roadmap")
        except (DeliveryContractError, RoadmapSourceError) as exc:
            raise HTTPException(409, "Accepted contract or roadmap unavailable") from exc
        return project, context

    @router.post("/api/projects/{project_id}/deliveries/{work_item_id}/prepare")
    def prepare(project_id: str, work_item_id: str):
        project, context = accepted(project_id, work_item_id)
        try:
            if context.mode is DeliveryMode.RELEASE:
                tip = workflow.prepare_release(project, context)
                branch = context.release_branch
            else:
                tip = workflow.prepare_hotfix(project, context)
                branch = context.expected_work_branch
            return {"work_item": context.work_item_id, "mode": context.mode.value,
                    "branch": branch, "head_sha": tip,
                    "delivery_fingerprint": context.fingerprint()}
        except ReleaseWorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/projects/{project_id}/deliveries/{work_item_id}/hotfix-pr")
    def open_pr(project_id: str, work_item_id: str, body: HotfixPrRequest):
        project, context = accepted(project_id, work_item_id)
        if context.mode is not DeliveryMode.HOTFIX:
            raise HTTPException(409, "Only HOTFIX may open a release-targeted PR")
        try:
            result = workflow.ensure_hotfix_pr(
                project, context, title=body.title, description=body.description,
                expected_head_sha=body.expected_head_sha,
            )
            return {"number": result.number, "base": result.base,
                    "head_sha": result.head_sha, "url": result.url,
                    "created": result.created}
        except ReleaseWorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/projects/{project_id}/deliveries/{work_item_id}/artifact")
    def inspect(project_id: str, work_item_id: str, tag: str,
                integrated_sha: str, validation_check: str):
        project, context = accepted(project_id, work_item_id)
        if context.mode is not DeliveryMode.HOTFIX:
            raise HTTPException(409, "HOTFIX contract required")
        try:
            result = workflow.inspect_artifact(
                project, context, tag=tag, integrated_sha=integrated_sha,
                validation_check=validation_check,
            )
            return {"tag": result.tag, "source_sha": result.source_sha,
                    "image": result.image, "digest": result.digest,
                    "validation_check": result.validation_check,
                    "deployment": "NOT_PERFORMED"}
        except ReleaseWorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
