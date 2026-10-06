from __future__ import annotations

from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.roadmaps import RoadmapIssue, RoadmapSourceError
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.project import Project
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
DONE | WORK | DONE | #1 | MAIN | Delivered foundation | - | -
NOW | WORK | READY | #1 | MAIN | Current delivery | - | DONE
PAR | WORK | READY | #1 | PARALLEL | Parallel delivery | - | DONE
NEXT | WORK | BLOCKED | #1 | MAIN | Next delivery | - | NOW
<!-- /COCKPIT_PIPELINE_V3 -->"""
ARCH_ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
ARCH | ARCHITECTURE_GATE | READY | #1 | MAIN | Architecture gate | - | -
NEXT | WORK | BLOCKED | #1 | MAIN | Next delivery | - | ARCH
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def __init__(self, body: str = ROADMAP):
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            "2026-10-04T20:00:00Z",
        )


class FailingRoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        raise RoadmapSourceError("unavailable")


class EvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


def application(tmp_path, *, roadmap_reader=None):
    settings = Settings(
        execution_poll_seconds=0,
        max_parallel_dev_executions=2,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'cockpit.db'}",
    )
    upgrade_database(settings)
    return create_app(
        settings,
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=roadmap_reader or RoadmapReader(),
        execution_reader=EvidenceReader(),
    )


def test_cockpit_overview_exposes_backend_derived_horizons_and_role_shell(tmp_path):
    app = application(tmp_path)
    response = TestClient(app).get("/api/projects/DevCockpit/cockpit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["project_id"] == "DevCockpit"
    assert payload["sources"]["roadmap"]["status"] == "available"
    assert payload["sources"]["roadmap"]["revision"]
    assert payload["horizons"]["now"]["key"] == "NOW"
    assert [item["key"] for item in payload["horizons"]["parallel"]] == ["PAR"]
    assert payload["horizons"]["next"]["key"] == "NEXT"
    assert [role["role"] for role in payload["roles"]] == ["PO", "ARCH", "REVIEWER"]
    assert payload["dev_pool"] == {
        "capacity_limit": 2,
        "capacity_used": 0,
        "capacity_available": 2,
        "candidates": 2,
        "active": 0,
        "waiting_for_capacity": 0,
        "waiting_for_resource_lock": 0,
        "items": [
            {
                "work_item_id": "NOW",
                "agent_session": "DevCockpit:DEV:NOW",
                "slot_state": "SELECTED",
                "execution_state": "READY",
                "ci_state": None,
                "interaction": None,
                "watchdog_stale": False,
                "watchdog_relaunch_prepared": False,
                "github_watchdog_kind": None,
                "github_watchdog_due": False,
                "github_watchdog_deadline_at": None,
                "github_watchdog_recovery_state": None,
            },
            {
                "work_item_id": "PAR",
                "agent_session": "DevCockpit:DEV:PAR",
                "slot_state": "SELECTED",
                "execution_state": "READY",
                "ci_state": None,
                "interaction": None,
                "watchdog_stale": False,
                "watchdog_relaunch_prepared": False,
                "github_watchdog_kind": None,
                "github_watchdog_due": False,
                "github_watchdog_deadline_at": None,
                "github_watchdog_recovery_state": None,
            },
        ],
    }


def test_cockpit_get_is_read_only_and_does_not_start_dev_work(tmp_path):
    app = application(tmp_path)
    client = TestClient(app)

    overview = client.get("/api/projects/DevCockpit/cockpit")
    assert overview.status_code == 200
    assert not prepare_prompt_deliveries_for_send(uow_factory=app.state.uow_factory)

    executions = client.get("/api/projects/DevCockpit/executions").json()
    assert executions["capacity"] == {"limit": 2, "used": 0, "available": 2}

    evaluation = client.post("/api/projects/DevCockpit/executions/evaluate")
    assert evaluation.status_code == 200
    assert [item["work_item_id"] for item in evaluation.json()["prompt_dispatches"]] == [
        "NOW",
        "PAR",
    ]


def test_ready_architecture_gate_is_visible_without_creating_a_dispatch(tmp_path):
    app = application(tmp_path, roadmap_reader=RoadmapReader(ARCH_ROADMAP))
    client = TestClient(app)

    response = client.get("/api/projects/DevCockpit/cockpit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["horizons"]["now"]["key"] == "ARCH"
    architect = next(role for role in payload["roles"] if role["role"] == "ARCH")
    assert architect["state"] == "ACTION"
    assert architect["primary_work_item_id"] == "ARCH"
    assert not prepare_prompt_deliveries_for_send(uow_factory=app.state.uow_factory)


def test_cockpit_degrades_explicitly_when_roadmap_is_unavailable(tmp_path):
    app = application(tmp_path, roadmap_reader=FailingRoadmapReader())
    response = TestClient(app).get("/api/projects/DevCockpit/cockpit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]["roadmap"] == {
        "status": "unavailable",
        "code": "GITHUB_UNAVAILABLE",
        "updated_at": None,
        "revision": None,
        "diagnostics": [],
    }
    assert payload["sources"]["executions"]["status"] == "unavailable"
    assert payload["sources"]["attention"]["status"] == "unavailable"
    assert payload["attention"]["state"] == "UNAVAILABLE"
    assert payload["horizons"] == {"now": None, "parallel": [], "next": None}
