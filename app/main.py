from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.application.executions import (
    ExecutionEvidenceReader,
    evaluate_project_execution,
    read_project_execution,
)
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import (
    AcknowledgementResult,
    acknowledge_prompt_delivery,
    prepare_prompt_deliveries_for_send,
)
from app.application.roadmaps import (
    ProjectRoadmapProjection,
    RoadmapIssueReader,
    RoadmapSourceError,
    read_project_roadmap,
)
from app.config import Settings, get_settings
from app.domain.execution import ExecutionProjection
from app.domain.project import Project
from app.domain.roadmap import PipelineDiagnostic, PipelineParseResult, WorkItem
from app.infrastructure.database import build_engine, build_session_factory
from app.infrastructure.github_execution import GitHubExecutionReader
from app.infrastructure.github_roadmaps import GitHubRoadmapReader
from app.infrastructure.project_config import load_projects
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork
from app.infrastructure.websocket_transport import (
    AckMessage,
    CompanionConnectionManager,
    PingMessage,
    ProtocolMessageError,
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


def _work_item_payload(item: WorkItem) -> dict[str, str]:
    return {
        "key": item.key,
        "type": item.type.value,
        "status": item.status.value,
        "parent": item.parent,
        "lane": item.lane,
        "title": item.title,
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
                "merged": pull_request.merged,
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


def create_app(
    settings: Settings | None = None,
    *,
    project_catalog: ProjectCatalog | None = None,
    roadmap_reader: RoadmapIssueReader | None = None,
    execution_reader: ExecutionEvidenceReader | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    engine = build_engine(active_settings)
    session_factory = build_session_factory(engine)
    connection_manager = CompanionConnectionManager()
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

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    async def execution_poller() -> None:
        while True:
            await asyncio.sleep(active_settings.execution_poll_seconds)
            for project in active_project_catalog.list():
                try:
                    await asyncio.to_thread(
                        evaluate_project_execution,
                        project,
                        roadmap_reader=active_roadmap_reader,
                        evidence_reader=active_execution_reader,
                        uow_factory=uow_factory,
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
                    "source": {"status": "unavailable", "code": exc.code},
                    "pipeline": None,
                },
            )
        return _roadmap_payload(projection)

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
        evaluation = evaluate_project_execution(
            project,
            roadmap_reader=active_roadmap_reader,
            evidence_reader=active_execution_reader,
            uow_factory=uow_factory,
        )
        payload = _execution_payload(project, evaluation.projection)
        payload["prompt_dispatch"] = (
            {
                "dispatch_id": str(evaluation.dispatch.dispatch_id),
                "agent_session": evaluation.dispatch.agent_session,
                "idempotency_key": evaluation.dispatch.idempotency_key,
            }
            if evaluation.dispatch is not None
            else None
        )
        return payload

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
                    await websocket.send_json(build_prompt_message(delivery))
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
                    await websocket.send_json(build_error_message(exc.code))
                    continue

                if isinstance(message, AckMessage):
                    result = acknowledge_prompt_delivery(
                        message.delivery_id,
                        uow_factory=uow_factory,
                    )
                    if result is AcknowledgementResult.UNKNOWN:
                        await websocket.send_json(build_error_message("unknown_delivery_ack"))
                    continue

                if isinstance(message, PingMessage):
                    await websocket.send_json(build_pong_message())
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
