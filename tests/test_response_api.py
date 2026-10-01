from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.application.chatgpt_responses import ImportChatGptResponseCommand, import_chatgpt_response
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.config import Settings
from app.domain.project import Project
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


def test_project_responses_api_returns_correlated_read_only_projection(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'response-api.db'}",
        execution_poll_seconds=0,
    )
    upgrade_database(settings)
    application = create_app(settings, project_catalog=ProjectCatalog([PROJECT]))
    factory = application.state.uow_factory
    dispatch = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id="DC-030",
            role="DEV",
            prompt_text="prompt",
            idempotency_key="response-api",
        ),
        uow_factory=factory,
    )
    delivery = prepare_prompt_deliveries_for_send(uow_factory=factory)[0]
    import_chatgpt_response(
        ImportChatGptResponseCommand(
            response_id=UUID("10cd3422-3dbd-481f-a5b0-5915a1f7f5be"),
            delivery_id=delivery.delivery_id,
            session=dispatch.agent_session,
            text="Full returned response\nwith code and lists.",
        ),
        uow_factory=factory,
    )

    with TestClient(application) as client:
        response = client.get("/api/projects/DevCockpit/responses")

    assert response.status_code == 200
    item = response.json()["responses"][0]
    assert item["response_id"] == "10cd3422-3dbd-481f-a5b0-5915a1f7f5be"
    assert item["delivery_id"] == str(delivery.delivery_id)
    assert item["session"] == "DevCockpit:DEV:DC-030"
    assert item["project_id"] == "DevCockpit"
    assert item["work_item_id"] == "DC-030"
    assert item["role"] == "DEV"
    assert item["text"] == "Full returned response\nwith code and lists."
    assert item["imported_at"].endswith("+00:00")


def test_project_responses_api_rejects_unknown_project(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'response-api-unknown.db'}",
        execution_poll_seconds=0,
    )
    upgrade_database(settings)
    application = create_app(settings, project_catalog=ProjectCatalog([PROJECT]))
    with TestClient(application) as client:
        response = client.get("/api/projects/Unknown/responses")
    assert response.status_code == 404
