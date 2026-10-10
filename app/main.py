from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api.architecture_gates import build_architecture_gate_router
from app.api.attention import build_attention_router
from app.api.cockpit import build_cockpit_router
from app.api.flow_analytics import build_flow_analytics_router
from app.api.orchestration import build_orchestration_router
from app.api.releases import build_release_router
from app.application.chatgpt_prompt_sends import (
    ChatGptSendStatusError,
    RecordChatGptSendStatusCommand,
    record_chatgpt_send_status,
)
from app.application.chatgpt_responses import (
    ChatGptResponseImportError,
    ImportChatGptResponseCommand,
    ResponseEchoesPromptError,
    ResponseIdConflictError,
    ResponseSessionMismatchError,
    UnknownPromptDeliveryError,
    import_chatgpt_response,
    list_imported_chatgpt_responses,
)
from app.application.executions import (
    ExecutionEvidenceReader,
    evaluate_project_execution,
    read_project_execution,
)
from app.application.flow_analytics import FlowAnalyticsEvidenceReader
from app.application.parallel_executions import (
    DevExecutionItem,
    ParallelDevExecutionProjection,
    evaluate_project_parallel_dev_executions,
    read_project_parallel_dev_executions,
)
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import (
    AcknowledgementResult,
    PromptRedeliveryBindingInvalidated,
    PromptRedeliveryDeliveryNotFound,
    PromptRedeliveryDispatchNotFound,
    PromptRedeliveryDispatchNotPrepared,
    PromptRedeliveryRequiresAcknowledgement,
    acknowledge_prompt_delivery,
    prepare_acknowledged_prompt_redelivery,
    prepare_prompt_deliveries_for_send,
)
from app.application.roadmaps import (
    ProjectRoadmapProjection,
    RoadmapIssueReader,
    RoadmapSourceError,
    read_project_roadmap,
)
from app.application.schedulers import ProjectSchedulerProjection, read_project_scheduler
from app.config import Settings, get_settings
from app.domain.execution import ExecutionProjection
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatchStatus
from app.domain.roadmap import PipelineDiagnostic, PipelineParseResult, WorkItem
from app.infrastructure.database import build_engine, build_session_factory
from app.infrastructure.github_architecture import GitHubArchitectureDocumentReader
from app.infrastructure.github_execution import GitHubExecutionReader
from app.infrastructure.github_finalization import GitHubPullRequestFinalizer
from app.infrastructure.github_release_workflow import GitHubReleaseWorkflow
from app.infrastructure.github_flow_analytics import GitHubFlowAnalyticsReader
from app.infrastructure.github_issues import GitHubIssueReader
from app.infrastructure.github_review import GitHubReviewReader
from app.infrastructure.github_roadmaps import GitHubRoadmapReader
from app.infrastructure.github_roadmap_writer import (
    GitHubIssueMappingReader,
    GitHubRoadmapWriter,
)
from app.infrastructure.project_config import load_projects
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork
from app.infrastructure.websocket_transport import (
    AckMessage,
    ChatGptResponseMessage,
    ChatGptSendStatusMessage,
    CompanionConnectionManager,
    PingMessage,
    ProtocolMessageError,
    build_chatgpt_response_ack,
    build_chatgpt_send_status_ack,
    build_error_message,
    build_pong_message,
    build_prompt_message,
    parse_inbound_message,
)


_DELIVERY_POLL_SECONDS = 0.1
_LOGGER = logging.getLogger(__name__)


def _project_payload(project: Project) -> dict[str, object]:
    return {
        "project_id": project.project_id,
        "repository_full_name": project.repository_full_name,
        "roadmap_issue_number": project.roadmap_issue_number,
    }


def _roadmap_error_source_payload(exc: RoadmapSourceError) -> dict[str, object]:
    return {
        "status": "unavailable",
        "code": exc.code,
        **exc.source_details(),
    }


def _work_item_payload(item: WorkItem) -> dict[str, object]:
    return {
        "key": item.key,
        "type": item.type.value,
        "status": item.status.value,
        "parent": item.parent,
        "lane": item.lane,
        "title": item.title,
        "replaces": item.replaces,
        "depends_on": list(item.depends_on),
    }


def _diagnostic_payload(diagnostic: PipelineDiagnostic) -> dict[str, object]:
    return {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "line_number": diagnostic.line_number,
    }


def _pipeline_payload(pipeline: PipelineParseResult) -> dict[str, object]:
    return {
        "valid": pipeline.valid,
        "version": pipeline.version,
        "work_items": [_work_item_payload(item) for item in pipeline.work_items],
        "diagnostics": [_diagnostic_payload(item) for item in pipeline.diagnostics],
        "active_ready_item": (
            _work_item_payload(pipeline.active_ready_item)
            if pipeline.active_ready_item is not None
            else None
        ),
    }


def _roadmap_payload(projection: ProjectRoadmapProjection) -> dict[str, object]:
    return {
        "project": _project_payload(projection.project),
        "source": {
            "status": "available",
            "repository_full_name": projection.issue.repository_full_name,
            "issue_number": projection.issue.issue_number,
            "updated_at": projection.issue.updated_at,
        },
        "pipeline": _pipeline_payload(projection.pipeline),
    }


def _scheduler_payload(projection: ProjectSchedulerProjection) -> dict[str, object]:
    scheduler = projection.scheduler
    return {
        "project": _project_payload(projection.project),
        "source": {
            "status": "available",
            "repository_full_name": projection.issue.repository_full_name,
            "issue_number": projection.issue.issue_number,
            "updated_at": projection.issue.updated_at,
        },
        "scheduler": {
            "valid": scheduler.valid,
            "pipeline_version": scheduler.pipeline_version,
            "executable_candidates": list(scheduler.executable_candidates),
            "selected_candidate": scheduler.selected_candidate,
            "diagnostics": [
                {
                    "code": diagnostic.code,
                    "message": diagnostic.message,
                    "line_number": diagnostic.line_number,
                }
                for diagnostic in scheduler.diagnostics
            ],
            "work_items": [
                {
                    "key": item.work_item.key,
                    "type": item.work_item.type.value,
                    "canonical_status": item.work_item.status.value,
                    "dependencies": list(item.dependencies),
                    "unsatisfied_dependencies": list(item.unsatisfied_dependencies),
                    "scheduler_state": item.state.value,
                    "executable": item.executable,
                    "reason": item.reason.value,
                    "expected_role": item.expected_role,
                    "next_action": item.next_action.value,
                }
                for item in scheduler.items
            ],
        },
    }


def _execution_payload(
    project: Project,
    projection: ExecutionProjection,
) -> dict[str, object]:
    pull_request = projection.pull_request
    ci = projection.ci
    return {
        "project": _project_payload(project),
        "work_item": (
            _work_item_payload(projection.work_item)
            if projection.work_item is not None
            else None
        ),
        "execution_state": projection.state.value,
        "next_action": projection.next_action.value,
        "branch": projection.branch.name if projection.branch is not None else (
            pull_request.branch if pull_request is not None else None
        ),
        "pull_request": (
            {
                "number": pull_request.number,
                "title": pull_request.title,
                "url": pull_request.url,
                "mergeable": pull_request.mergeable,
                "mergeable_state": pull_request.mergeable_state,
                "merged": pull_request.merged,
                "auto_merge_enabled": pull_request.auto_merge_enabled,
                "base_branch": pull_request.base_branch,
                "base_sha": pull_request.base_sha,
                "behind_by": pull_request.behind_by,
            }
            if pull_request is not None
            else None
        ),
        "head_sha": (
            pull_request.head_sha
            if pull_request is not None
            else (projection.branch.sha if projection.branch is not None else None)
        ),
        "ci": (
            {
                "state": ci.state.value,
                "observed_runs": ci.observed_runs,
                "failed_jobs": list(ci.failed_jobs),
                "runs": [
                    {
                        "run_id": run.run_id,
                        "name": run.name,
                        "status": run.status,
                        "conclusion": run.conclusion,
                        "attempt": run.attempt,
                        "url": run.url,
                    }
                    for run in ci.runs
                ],
            }
            if ci is not None
            else None
        ),
        "diagnostics": [
            {"code": item.code, "message": item.message}
            for item in projection.diagnostics
        ],
    }


def _resource_lock_payload(lock) -> dict[str, object]:
    return {
        "lock_id": str(lock.lock_id),
        "surface": lock.surface.key,
        "mode": lock.mode.value,
        "state": lock.state.value,
        "work_item_id": lock.work_item_id,
        "agent_session": lock.agent_session,
        "lease_expires_at": lock.lease_expires_at.isoformat(),
        "version": lock.version,
        "released_at": (
            lock.released_at.isoformat()
            if lock.released_at is not None
            else None
        ),
        "release_reason": lock.release_reason,
    }


def _parallel_execution_item_payload(
    project: Project,
    item: DevExecutionItem,
) -> dict[str, object]:
    payload = _execution_payload(project, item.execution)
    payload.pop("project", None)
    return {
        "role": "DEV",
        "agent_session": item.agent_session,
        "scheduler": {
            "state": item.scheduler.state.value,
            "reason": item.scheduler.reason.value,
            "dependencies": list(item.scheduler.dependencies),
            "unsatisfied_dependencies": list(item.scheduler.unsatisfied_dependencies),
        },
        "slot_state": item.slot_state.value,
        "active": item.active,
        "waiting_for_capacity": item.waiting_for_capacity,
        "waiting_for_resource_lock": item.waiting_for_resource_lock,
        "inhibition_reason": item.inhibition_reason,
        "interaction": (
            {
                "project_id": item.interaction.project_id,
                "work_item_id": item.interaction.work_item_id,
                "role": item.interaction.role,
                "agent_session": item.interaction.agent_session,
                "dispatch_id": item.interaction.dispatch_id,
                "dispatch_status": item.interaction.dispatch_status,
                "delivery_id": item.interaction.delivery_id,
                "delivery_status": item.interaction.delivery_status,
                "delivery_acknowledged_at": (
                    item.interaction.delivery_acknowledged_at.isoformat()
                    if item.interaction.delivery_acknowledged_at is not None
                    else None
                ),
                "state": item.interaction.state,
                "send_state": item.interaction.send_state,
                "send_attempt_count": item.interaction.send_attempt_count,
                "send_error_code": item.interaction.send_error_code,
                "send_confirmed_at": (
                    item.interaction.send_confirmed_at.isoformat()
                    if item.interaction.send_confirmed_at is not None
                    else None
                ),
                "imported_response_available": (
                    item.interaction.imported_response_available
                ),
                "imported_response_count": item.interaction.imported_response_count,
                "latest_imported_response_at": (
                    item.interaction.latest_imported_response_at.isoformat()
                    if item.interaction.latest_imported_response_at is not None
                    else None
                ),
                "manual_send_required": item.interaction.manual_send_required,
                "automatic_resend_allowed": item.interaction.automatic_resend_allowed,
            }
            if item.interaction is not None
            else None
        ),
        "github_watchdog": (
            {
                "kind": item.github_watchdog.kind.value,
                "last_activity_at": item.github_watchdog.last_activity_at,
                "threshold_seconds": item.github_watchdog.threshold_seconds,
                "deadline_at": item.github_watchdog.deadline_at.isoformat(),
                "due": item.github_watchdog.due,
                "recovery_prepared": item.github_watchdog.recovery_prepared,
                "recovery_state": item.github_watchdog.recovery_state,
                "evidence_identity": item.github_watchdog.evidence_identity,
            }
            if item.github_watchdog is not None
            else None
        ),
        "watchdog": (
            {
                "branch_last_activity_at": item.watchdog.branch_last_activity_at,
                "threshold_seconds": item.watchdog.threshold_seconds,
                "send_confirmed_at": (
                    item.watchdog.send_confirmed_at.isoformat()
                    if item.watchdog.send_confirmed_at is not None
                    else None
                ),
                "deadline_at": (
                    item.watchdog.deadline_at.isoformat()
                    if item.watchdog.deadline_at is not None
                    else None
                ),
                "stale_due": item.watchdog.stale_due,
                "relaunch_prepared": item.watchdog.relaunch_prepared,
            }
            if item.watchdog is not None
            else None
        ),
        "resource_locks": {
            "required": [
                {
                    "surface": requirement.surface.key,
                    "mode": requirement.mode.value,
                }
                for requirement in item.required_locks
            ],
            "held": [
                _resource_lock_payload(lock)
                for lock in item.lock_records
                if lock.state.value == "ACTIVE"
            ],
            "records": [
                _resource_lock_payload(lock)
                for lock in item.lock_records
            ],
            "conflict": (
                {
                    "surface": item.lock_conflict.surface.key,
                    "requested_mode": item.lock_conflict.requested_mode.value,
                    "holder_work_item_id": item.lock_conflict.holder_work_item_id,
                    "holder_agent_session": item.lock_conflict.holder_agent_session,
                    "holder_mode": item.lock_conflict.holder_mode.value,
                    "holder_state": item.lock_conflict.holder_state.value,
                    "reason": item.lock_conflict.reason,
                }
                if item.lock_conflict is not None
                else None
            ),
            "recovery_state": item.lock_recovery_state,
        },
        **payload,
    }


def _parallel_executions_payload(
    projection: ParallelDevExecutionProjection,
) -> dict[str, object]:
    return {
        "project": _project_payload(projection.project),
        "source": {
            "status": "available",
            "repository_full_name": projection.issue.repository_full_name,
            "issue_number": projection.issue.issue_number,
            "updated_at": projection.issue.updated_at,
        },
        "capacity": {
            "limit": projection.max_parallel_dev_executions,
            "used": projection.active_count,
            "available": projection.available_capacity,
        },
        "executable_candidates": list(projection.scheduler.executable_candidates),
        "executions": [
            _parallel_execution_item_payload(projection.project, item)
            for item in projection.items
        ],
    }



def _response_payload(response) -> dict[str, object]:
    return {
        "response_id": str(response.response_id),
        "delivery_id": str(response.delivery_id),
        "session": response.session,
        "project_id": response.project_id,
        "work_item_id": response.work_item_id,
        "role": response.role,
        "imported_at": response.imported_at.isoformat(),
        "text": response.text,
    }


def create_app(
    settings: Settings | None = None,
    *,
    project_catalog: ProjectCatalog | None = None,
    roadmap_reader: RoadmapIssueReader | None = None,
    execution_reader: ExecutionEvidenceReader | None = None,
    execution_finalizer=None,
    flow_analytics_reader: FlowAnalyticsEvidenceReader | None = None,
    roadmap_writer=None,
    issue_mapping_reader=None,
    github_issue_reader=None,
    architecture_document_reader=None,
    review_reader=None,
) -> FastAPI:
    active_settings = settings or get_settings()
    engine = build_engine(active_settings)
    session_factory = build_session_factory(engine)
    connection_manager = CompanionConnectionManager()
    resource_lock_lease_owner_id = str(uuid4())
    active_project_catalog = project_catalog or ProjectCatalog(
        load_projects(active_settings.projects_config_path)
    )
    token = (
        active_settings.github_token.get_secret_value()
        if active_settings.github_token is not None
        else None
    )
    active_roadmap_reader = roadmap_reader or GitHubRoadmapReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_execution_reader = execution_reader or GitHubExecutionReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_execution_finalizer = execution_finalizer or GitHubPullRequestFinalizer(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_release_workflow = GitHubReleaseWorkflow(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_flow_analytics_reader = flow_analytics_reader or GitHubFlowAnalyticsReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_roadmap_writer = roadmap_writer or GitHubRoadmapWriter(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_issue_mapping_reader = issue_mapping_reader or GitHubIssueMappingReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_github_issue_reader = github_issue_reader or GitHubIssueReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )
    active_architecture_document_reader = (
        architecture_document_reader
        or GitHubArchitectureDocumentReader(
            token=token,
            timeout_seconds=active_settings.github_timeout_seconds,
        )
    )
    active_review_reader = review_reader or GitHubReviewReader(
        token=token,
        timeout_seconds=active_settings.github_timeout_seconds,
    )

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    async def execution_poller() -> None:
        while True:
            await asyncio.sleep(active_settings.execution_poll_seconds)
            for project in active_project_catalog.list():
                try:
                    await asyncio.to_thread(
                        evaluate_project_parallel_dev_executions,
                        project,
                        roadmap_reader=active_roadmap_reader,
                        evidence_reader=active_execution_reader,
                        uow_factory=uow_factory,
                        max_parallel_dev_executions=active_settings.max_parallel_dev_executions,
                        resource_lock_lease_seconds=active_settings.resource_lock_lease_seconds,
                        dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
                        pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
                        ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
                        auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
                        lease_owner_id=resource_lock_lease_owner_id,
                        finalizer=active_execution_finalizer,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.warning(
                        "Execution polling failed for project %s: %s",
                        project.project_id,
                        type(exc).__name__,
                    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        poller_task: asyncio.Task[None] | None = None
        if active_settings.execution_poll_seconds > 0:
            poller_task = asyncio.create_task(execution_poller())
        try:
            yield
        finally:
            if poller_task is not None:
                poller_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poller_task
            engine.dispose()

    application = FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.uow_factory = uow_factory
    application.state.companion_connections = connection_manager
    application.state.project_catalog = active_project_catalog
    application.state.roadmap_reader = active_roadmap_reader
    application.state.execution_reader = active_execution_reader
    application.state.execution_finalizer = active_execution_finalizer
    application.state.release_workflow = active_release_workflow
    application.state.flow_analytics_reader = active_flow_analytics_reader
    application.state.roadmap_writer = active_roadmap_writer
    application.state.issue_mapping_reader = active_issue_mapping_reader
    application.state.github_issue_reader = active_github_issue_reader
    application.state.architecture_document_reader = active_architecture_document_reader
    application.state.review_reader = active_review_reader

    application.include_router(build_release_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        uow_factory=uow_factory,
        workflow=active_release_workflow,
    ))

    application.include_router(build_architecture_gate_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        uow_factory=uow_factory,
    ))

    application.include_router(build_orchestration_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        roadmap_writer=active_roadmap_writer,
        issue_mapping_reader=active_issue_mapping_reader,
        evidence_reader=active_execution_reader,
        uow_factory=uow_factory,
    ))

    application.include_router(build_attention_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        evidence_reader=active_execution_reader,
        uow_factory=uow_factory,
        max_parallel_dev_executions=active_settings.max_parallel_dev_executions,
        companion_connections=connection_manager,
        dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
        pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
        ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
        auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
    ))

    application.include_router(build_cockpit_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        evidence_reader=active_execution_reader,
        issue_reader=active_github_issue_reader,
        architecture_document_reader=active_architecture_document_reader,
        review_reader=active_review_reader,
        uow_factory=uow_factory,
        max_parallel_dev_executions=active_settings.max_parallel_dev_executions,
        dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
        pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
        ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
        auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
        companion_connections=connection_manager,
    ))

    application.include_router(build_flow_analytics_router(
        project_catalog=active_project_catalog,
        roadmap_reader=active_roadmap_reader,
        analytics_reader=active_flow_analytics_reader,
    ))

    @application.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/projects", tags=["projects"])
    def projects() -> dict[str, object]:
        return {"projects": [_project_payload(project) for project in active_project_catalog.list()]}

    @application.get("/api/projects/{project_id}/roadmap", tags=["projects"])
    def project_roadmap(project_id: str):
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            projection = read_project_roadmap(project, reader=active_roadmap_reader)
        except RoadmapSourceError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "project": _project_payload(project),
                    "source": _roadmap_error_source_payload(exc),
                    "pipeline": None,
                },
            )
        return _roadmap_payload(projection)

    @application.get("/api/projects/{project_id}/scheduler", tags=["projects"])
    def project_scheduler(project_id: str):
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            projection = read_project_scheduler(project, reader=active_roadmap_reader)
        except RoadmapSourceError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "project": _project_payload(project),
                    "source": _roadmap_error_source_payload(exc),
                    "scheduler": None,
                },
            )
        return _scheduler_payload(projection)

    @application.get("/api/projects/{project_id}/executions", tags=["projects"])
    def project_executions(project_id: str):
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            projection = read_project_parallel_dev_executions(
                project,
                roadmap_reader=active_roadmap_reader,
                evidence_reader=active_execution_reader,
                uow_factory=uow_factory,
                max_parallel_dev_executions=active_settings.max_parallel_dev_executions,
                dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
                pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
                ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
                auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
            )
        except RoadmapSourceError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "project": _project_payload(project),
                    "source": _roadmap_error_source_payload(exc),
                    "capacity": None,
                    "executable_candidates": [],
                    "executions": [],
                },
            )
        return _parallel_executions_payload(projection)

    @application.post("/api/projects/{project_id}/executions/evaluate", tags=["projects"])
    def evaluate_executions(project_id: str) -> dict[str, object]:
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        evaluation = evaluate_project_parallel_dev_executions(
            project,
            roadmap_reader=active_roadmap_reader,
            evidence_reader=active_execution_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=active_settings.max_parallel_dev_executions,
            resource_lock_lease_seconds=active_settings.resource_lock_lease_seconds,
            dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
            pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
            ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
            auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
            lease_owner_id=resource_lock_lease_owner_id,
            finalizer=active_execution_finalizer,
        )
        payload = _parallel_executions_payload(evaluation.projection)
        payload["prompt_dispatches"] = [
            {
                "dispatch_id": str(dispatch.dispatch_id),
                "work_item_id": dispatch.work_item_id,
                "agent_session": dispatch.agent_session,
                "idempotency_key": dispatch.idempotency_key,
            }
            for dispatch in evaluation.dispatches
        ]
        return payload

    @application.get("/api/projects/{project_id}/responses", tags=["projects"])
    def project_responses(project_id: str) -> dict[str, object]:
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        responses = list_imported_chatgpt_responses(
            project.project_id,
            uow_factory=uow_factory,
        )
        return {"responses": [_response_payload(response) for response in responses]}

    @application.get("/api/projects/{project_id}/execution", tags=["projects"])
    def project_execution(project_id: str) -> dict[str, object]:
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        projection = read_project_execution(
            project,
            roadmap_reader=active_roadmap_reader,
            evidence_reader=active_execution_reader,
        )
        return _execution_payload(project, projection)

    @application.post("/api/projects/{project_id}/execution/evaluate", tags=["projects"])
    def evaluate_execution(project_id: str) -> dict[str, object]:
        project = active_project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        # Legacy single-execution mutation endpoint delegates to the same
        # ResourceLock-aware gate. It cannot bypass DC-052 acquisition.
        evaluation = evaluate_project_parallel_dev_executions(
            project,
            roadmap_reader=active_roadmap_reader,
            evidence_reader=active_execution_reader,
            uow_factory=uow_factory,
            max_parallel_dev_executions=1,
            resource_lock_lease_seconds=active_settings.resource_lock_lease_seconds,
            dev_stale_after_seconds=active_settings.dev_stale_after_seconds,
            pr_no_ci_after_seconds=active_settings.pr_no_ci_after_seconds,
            ci_stall_after_seconds=active_settings.ci_stall_after_seconds,
            auto_merge_grace_seconds=active_settings.auto_merge_grace_seconds,
            lease_owner_id=resource_lock_lease_owner_id,
            finalizer=active_execution_finalizer,
        )
        primary = evaluation.projection.items[0] if evaluation.projection.items else None
        if primary is None:
            projection = read_project_execution(
                project,
                roadmap_reader=active_roadmap_reader,
                evidence_reader=active_execution_reader,
            )
            payload = _execution_payload(project, projection)
            payload["prompt_dispatch"] = None
            return payload

        payload = _execution_payload(project, primary.execution)
        dispatch = next(
            (
                item
                for item in evaluation.dispatches
                if item.work_item_id == primary.scheduler.work_item.key
            ),
            None,
        )
        payload["prompt_dispatch"] = (
            {
                "dispatch_id": str(dispatch.dispatch_id),
                "agent_session": dispatch.agent_session,
                "idempotency_key": dispatch.idempotency_key,
            }
            if dispatch is not None
            else None
        )
        return payload

    @application.post("/api/prompt-dispatches/{dispatch_id}/redeliver", tags=["companion"])
    async def redeliver_prompt(dispatch_id: UUID) -> dict[str, object]:
        if not connection_manager.has_active_connection:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "COMPANION_NOT_CONNECTED",
                    "message": "No Firefox companion is currently connected.",
                },
            )

        try:
            delivery = prepare_acknowledged_prompt_redelivery(
                dispatch_id,
                uow_factory=uow_factory,
            )
        except PromptRedeliveryDispatchNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": exc.code, "message": "PromptDispatch not found."},
            ) from exc
        except (
            PromptRedeliveryDispatchNotPrepared,
            PromptRedeliveryDeliveryNotFound,
            PromptRedeliveryRequiresAcknowledgement,
            PromptRedeliveryBindingInvalidated,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": (
                        "Only an acknowledged PREPARED prompt with a safe conversation target "
                        "can be manually redelivered."
                    ),
                },
            ) from exc

        if not await connection_manager.send_json(build_prompt_message(delivery)):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "COMPANION_NOT_CONNECTED",
                    "message": "The Firefox companion disconnected before redelivery.",
                },
            )

        return {
            "status": "RESENT",
            "dispatch_id": str(delivery.dispatch_id),
            "delivery_id": str(delivery.delivery_id),
            "session": delivery.session,
            "attempt_count": delivery.attempt_count,
        }

    @application.websocket("/api/companion/ws")
    async def companion_websocket(websocket: WebSocket) -> None:
        if not await connection_manager.connect(websocket):
            return

        sent_on_connection: set = set()
        receive_task: asyncio.Task[str] | None = asyncio.create_task(websocket.receive_text())

        try:
            while True:
                outbound = prepare_prompt_deliveries_for_send(
                    uow_factory=uow_factory,
                    exclude_delivery_ids=sent_on_connection,
                )
                for delivery in outbound:
                    # Recheck each reserved outbound message under the same writer
                    # boundary used by Handoff cancellation. A prepared batch can
                    # have become superseded before this socket write.
                    with uow_factory() as delivery_uow:
                        dispatch = delivery_uow.prompt_dispatches.get(delivery.dispatch_id)
                        if dispatch is None or dispatch.status != PromptDispatchStatus.PREPARED:
                            continue
                        binding = delivery_uow.conversation_bindings.get_by_agent_session(
                            delivery.session
                        )
                        if binding is not None and binding.state.value == "INVALIDATED":
                            continue
                        if binding is None and delivery.routing is not None:
                            continue
                        if binding is not None and (
                            delivery.routing is None
                            or binding.version != delivery.routing.binding_version
                            or binding.conversation_id != delivery.routing.conversation_id
                            or binding.canonical_url != delivery.routing.canonical_url
                        ):
                            continue
                        if not await connection_manager.send_json(build_prompt_message(delivery)):
                            raise RuntimeError("companion disconnected during prompt send")
                        sent_on_connection.add(delivery.delivery_id)

                done, _ = await asyncio.wait(
                    {receive_task},
                    timeout=_DELIVERY_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue

                try:
                    raw_message = receive_task.result()
                except WebSocketDisconnect:
                    break
                receive_task = asyncio.create_task(websocket.receive_text())

                try:
                    message = parse_inbound_message(raw_message)
                except ProtocolMessageError as exc:
                    await connection_manager.send_json(build_error_message(exc.code))
                    continue


                if isinstance(message, ChatGptSendStatusMessage):
                    try:
                        record_chatgpt_send_status(
                            RecordChatGptSendStatusCommand(
                                event_id=message.event_id,
                                delivery_id=message.delivery_id,
                                session=message.session,
                                state=message.state,
                                attempt_count=message.attempt_count,
                                conversation_id=message.conversation_id,
                                canonical_url=message.canonical_url,
                                error_code=message.error_code,
                                next_retry_at=message.next_retry_at,
                                occurred_at=message.occurred_at,
                            ),
                            uow_factory=uow_factory,
                        )
                    except ChatGptSendStatusError as exc:
                        await connection_manager.send_json(build_error_message(exc.code))
                    else:
                        await connection_manager.send_json(
                            build_chatgpt_send_status_ack(message.event_id)
                        )
                    continue

                if isinstance(message, ChatGptResponseMessage):
                    try:
                        import_chatgpt_response(
                            ImportChatGptResponseCommand(
                                response_id=message.response_id,
                                delivery_id=message.delivery_id,
                                session=message.session,
                                text=message.text,
                            ),
                            uow_factory=uow_factory,
                        )
                    except UnknownPromptDeliveryError:
                        await connection_manager.send_json(
                            build_error_message(
                                "unknown_delivery",
                                response_id=message.response_id,
                            )
                        )
                    except ResponseSessionMismatchError:
                        await connection_manager.send_json(
                            build_error_message(
                                "session_mismatch",
                                response_id=message.response_id,
                            )
                        )
                    except ResponseIdConflictError:
                        await connection_manager.send_json(
                            build_error_message(
                                "response_id_conflict",
                                response_id=message.response_id,
                            )
                        )
                    except ResponseEchoesPromptError:
                        await connection_manager.send_json(
                            build_error_message(
                                "response_echoes_prompt",
                                response_id=message.response_id,
                            )
                        )
                    except ChatGptResponseImportError:
                        await connection_manager.send_json(
                            build_error_message(
                                "invalid_chatgpt_response",
                                response_id=message.response_id,
                            )
                        )
                    else:
                        await connection_manager.send_json(
                            build_chatgpt_response_ack(message.response_id)
                        )
                    continue

                if isinstance(message, AckMessage):
                    result = acknowledge_prompt_delivery(
                        message.delivery_id,
                        uow_factory=uow_factory,
                    )
                    if result is AcknowledgementResult.UNKNOWN:
                        await connection_manager.send_json(build_error_message("unknown_delivery_ack"))
                    continue

                if isinstance(message, PingMessage):
                    await connection_manager.send_json(build_pong_message())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await receive_task
            await connection_manager.disconnect(websocket)

    return application


app = create_app()
