from __future__ import annotations

from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.project import Project
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("RessourcePlanner", "tchi99/RessourcePlanner", 55)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
PREV | WORK | DONE | #1 | MAIN | Previous delivery | - | -
ASTRA-575 | ARCHITECTURE_GATE | READY | #575 | MAIN | Architecture assets | - | PREV
575A | WORK | BLOCKED | #575 | MAIN | Implementation | - | ASTRA-575
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            ROADMAP,
            "2026-10-02T20:00:00Z",
        )


class EvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


def application(tmp_path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'architecture-gate.db'}",
        execution_poll_seconds=0,
    )
    upgrade_database(settings)
    return create_app(
        settings,
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=RoadmapReader(),
        execution_reader=EvidenceReader(),
    )


def test_ready_architecture_gate_is_never_auto_dispatched(tmp_path):
    app = application(tmp_path)
    with TestClient(app) as client:
        evaluation = client.post("/api/projects/RessourcePlanner/executions/evaluate")
        assert evaluation.status_code == 200
        assert evaluation.json()["prompt_dispatches"] == []

        with app.state.uow_factory() as uow:
            assert [
                d for d in uow.prompt_dispatches.list_prepared()
                if d.project_id == "RessourcePlanner"
            ] == []

        attention = client.get("/api/projects/RessourcePlanner/attention")
        assert attention.status_code == 200
        items = attention.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "ARCHITECTURE_GATE_AUTHORIZATION"
        assert item["role"] == "ARCH"
        assert item["work_item_id"] == "ASTRA-575"
        assert item["primary_action"]["kind"] == "AUTHORIZE_ARCHITECTURE_GATE"
        assert item["context"]["human_authorization_required"] is True


def test_architecture_gate_requires_explicit_confirmation_and_is_idempotent(tmp_path):
    app = application(tmp_path)
    with TestClient(app) as client:
        missing_confirmation = client.post(
            "/api/projects/RessourcePlanner/architecture-gates/ASTRA-575/authorize",
            json={"confirm": False},
        )
        assert missing_confirmation.status_code == 422

        first = client.post(
            "/api/projects/RessourcePlanner/architecture-gates/ASTRA-575/authorize",
            json={"confirm": True},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["status"] == "AUTHORIZED"
        assert first_payload["agent_session"] == "RessourcePlanner:ARCH:ASTRA-575"

        second = client.post(
            "/api/projects/RessourcePlanner/architecture-gates/ASTRA-575/authorize",
            json={"confirm": True},
        )
        assert second.status_code == 200
        assert second.json()["dispatch_id"] == first_payload["dispatch_id"]

        with app.state.uow_factory() as uow:
            dispatches = [
                d for d in uow.prompt_dispatches.list_prepared()
                if d.project_id == "RessourcePlanner"
                and d.work_item_id == "ASTRA-575"
            ]
        assert len(dispatches) == 1
        assert dispatches[0].role.value == "ARCH"
        assert "Autorisation humaine : accordée explicitement dans DevCockpit" in dispatches[0].prompt_text

        attention = client.get("/api/projects/RessourcePlanner/attention")
        assert attention.status_code == 200
        items = attention.json()["items"]
        assert len(items) == 1
        assert items[0]["role"] == "ARCH"
        assert items[0]["kind"] == "TRANSPORT_BLOCKED"
        assert items[0]["work_item_id"] == "ASTRA-575"


def test_non_architecture_work_item_cannot_use_authorization_endpoint(tmp_path):
    app = application(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/RessourcePlanner/architecture-gates/PREV/authorize",
            json={"confirm": True},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "NOT_ARCHITECTURE_GATE"
