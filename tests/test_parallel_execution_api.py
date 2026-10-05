import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.project import Project
from app.domain.resource_lock import (
    ResourceLockMode,
    ResourceLockRequirement,
    WorkItemResourceLockDeclaration,
)
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | READY | #1 | MAIN | A | - | -
B | WORK | READY | #1 | AUX | B | - | -
C | WORK | READY | #1 | AUX2 | C | - | -
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            ROADMAP,
            "2026-10-02T12:00:00Z",
        )


class EvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


def test_parallel_execution_api_exposes_capacity_and_is_idempotent(tmp_path):
    settings = Settings(
        execution_poll_seconds=0,
        max_parallel_dev_executions=2,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'parallel.db'}",
    )
    upgrade_database(settings)
    app = create_app(
        settings,
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=RoadmapReader(),
        execution_reader=EvidenceReader(),
    )
    client = TestClient(app)

    read_only = client.get("/api/projects/DevCockpit/executions")
    assert read_only.status_code == 200
    read_payload = read_only.json()
    assert read_payload["capacity"] == {"limit": 2, "used": 0, "available": 2}
    assert [item["work_item"]["key"] for item in read_payload["executions"]] == [
        "A",
        "B",
        "C",
    ]
    assert [item["slot_state"] for item in read_payload["executions"]] == [
        "SELECTED",
        "SELECTED",
        "WAITING_FOR_CAPACITY",
    ]
    assert [item["agent_session"] for item in read_payload["executions"][:2]] == [
        "DevCockpit:DEV:A",
        "DevCockpit:DEV:B",
    ]

    assert all(item["interaction"] is None for item in read_payload["executions"])
    assert all(item["watchdog"] is None for item in read_payload["executions"])

    first = client.post("/api/projects/DevCockpit/executions/evaluate")
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["capacity"] == {"limit": 2, "used": 2, "available": 0}
    assert [item["work_item_id"] for item in first_payload["prompt_dispatches"]] == [
        "A",
        "B",
    ]
    assert [
        item["agent_session"] for item in first_payload["prompt_dispatches"]
    ] == [
        "DevCockpit:DEV:A",
        "DevCockpit:DEV:B",
    ]

    second = client.post("/api/projects/DevCockpit/executions/evaluate")
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["prompt_dispatches"] == []
    assert second_payload["capacity"] == {"limit": 2, "used": 2, "available": 0}
    states = {
        item["work_item"]["key"]: item["slot_state"]
        for item in second_payload["executions"]
    }
    assert states == {
        "A": "ACTIVE",
        "B": "ACTIVE",
        "C": "WAITING_FOR_CAPACITY",
    }
    active = {
        item["work_item"]["key"]: item
        for item in second_payload["executions"]
        if item["active"]
    }
    assert active["A"]["interaction"]["project_id"] == "DevCockpit"
    assert active["A"]["interaction"]["work_item_id"] == "A"
    assert active["A"]["interaction"]["agent_session"] == "DevCockpit:DEV:A"
    assert active["A"]["interaction"]["dispatch_status"] == "PREPARED"
    assert active["A"]["interaction"]["delivery_status"] is None
    assert active["A"]["interaction"]["state"] == "PROMPT_PREPARED"
    assert active["A"]["interaction"]["send_state"] is None
    assert active["A"]["interaction"]["imported_response_available"] is False
    assert active["A"]["interaction"]["imported_response_count"] == 0
    assert active["B"]["interaction"]["dispatch_status"] == "PREPARED"



@pytest.mark.parametrize("value", [0, -1, 33])
def test_parallel_execution_limit_is_validated(value):
    with pytest.raises(ValidationError):
        Settings(max_parallel_dev_executions=value)



LOCKED_PROJECT = Project(
    "DevCockpit",
    "jctrottier9-gif/DevCockpit",
    1,
    resource_locks=(
        WorkItemResourceLockDeclaration(
            "A",
            (ResourceLockRequirement.build("migration:alembic", ResourceLockMode.EXCLUSIVE),),
        ),
        WorkItemResourceLockDeclaration(
            "B",
            (ResourceLockRequirement.build("migration:alembic", ResourceLockMode.EXCLUSIVE),),
        ),
    ),
)


class LockedRoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | READY | #1 | MAIN | A | - | -
B | WORK | READY | #1 | AUX | B | - | -
<!-- /COCKPIT_PIPELINE_V3 -->""",
            "2026-10-02T12:00:00Z",
        )


def test_resource_lock_conflict_is_exposed_and_survives_application_restart(tmp_path):
    settings = Settings(
        execution_poll_seconds=0,
        max_parallel_dev_executions=2,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'locks-api.db'}",
    )
    upgrade_database(settings)

    first_app = create_app(
        settings,
        project_catalog=ProjectCatalog([LOCKED_PROJECT]),
        roadmap_reader=LockedRoadmapReader(),
        execution_reader=EvidenceReader(),
    )
    first_client = TestClient(first_app)
    first = first_client.post("/api/projects/DevCockpit/executions/evaluate")
    assert first.status_code == 200
    first_payload = first.json()
    assert [item["work_item_id"] for item in first_payload["prompt_dispatches"]] == ["A"]

    by_key = {
        item["work_item"]["key"]: item
        for item in first_payload["executions"]
    }
    assert by_key["A"]["slot_state"] == "ACTIVE"
    assert by_key["A"]["resource_locks"]["held"][0]["surface"] == "migration:alembic"
    assert by_key["B"]["slot_state"] == "WAITING_FOR_RESOURCE_LOCK"
    assert by_key["B"]["resource_locks"]["conflict"] == {
        "surface": "migration:alembic",
        "requested_mode": "EXCLUSIVE",
        "holder_work_item_id": "A",
        "holder_agent_session": "DevCockpit:DEV:A",
        "holder_mode": "EXCLUSIVE",
        "holder_state": "ACTIVE",
        "reason": "INCOMPATIBLE_RESOURCE_LOCK",
    }
    first_client.close()

    restarted_app = create_app(
        settings,
        project_catalog=ProjectCatalog([LOCKED_PROJECT]),
        roadmap_reader=LockedRoadmapReader(),
        execution_reader=EvidenceReader(),
    )
    restarted_client = TestClient(restarted_app)
    read_back = restarted_client.get("/api/projects/DevCockpit/executions")
    assert read_back.status_code == 200
    restarted_by_key = {
        item["work_item"]["key"]: item
        for item in read_back.json()["executions"]
    }
    assert restarted_by_key["A"]["active"] is True
    assert restarted_by_key["B"]["resource_locks"]["conflict"]["holder_work_item_id"] == "A"

    repoll = restarted_client.post("/api/projects/DevCockpit/executions/evaluate")
    assert repoll.status_code == 200
    assert repoll.json()["prompt_dispatches"] == []
    restarted_client.close()
