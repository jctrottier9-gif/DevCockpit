from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue, RoadmapRateLimitError, RoadmapSourceError
from app.config import Settings
from app.domain.project import Project
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
VALID_BODY = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
DC-020 | WORK | READY | #1 | MAIN | roadmap parser
DC-021 | WORK | BLOCKED | #1 | MAIN | execution projection
<!-- /COCKPIT_PIPELINE_V1 -->"""


class FakeReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(project.repository_full_name, project.roadmap_issue_number, VALID_BODY, "now")


class OfflineReader:
    def read(self, project: Project) -> RoadmapIssue:
        raise RoadmapSourceError("offline")


class RateLimitedReader:
    def read(self, project: Project) -> RoadmapIssue:
        raise RoadmapRateLimitError(
            "rate limited",
            status_code=403,
            token_configured=True,
            rate_limit_remaining="0",
            rate_limit_reset="1791339999",
        )


def client_for(reader) -> TestClient:
    app = create_app(
        Settings(),
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=reader,
    )
    return TestClient(app)


def test_projects_and_roadmap_api_expose_backend_projection() -> None:
    client = client_for(FakeReader())

    projects = client.get("/api/projects")
    roadmap = client.get("/api/projects/DevCockpit/roadmap")

    assert projects.status_code == 200
    assert projects.json()["projects"][0]["repository_full_name"] == "jctrottier9-gif/DevCockpit"
    assert roadmap.status_code == 200
    assert roadmap.json()["pipeline"]["valid"] is True
    assert roadmap.json()["pipeline"]["active_ready_item"]["key"] == "DC-020"


def test_github_failure_is_distinct_from_valid_pipeline_without_ready() -> None:
    response = client_for(OfflineReader()).get("/api/projects/DevCockpit/roadmap")

    assert response.status_code == 502
    assert response.json()["source"] == {
        "status": "unavailable",
        "code": "GITHUB_UNAVAILABLE",
    }
    assert response.json()["pipeline"] is None


def test_rate_limit_failure_exposes_safe_source_diagnostics() -> None:
    response = client_for(RateLimitedReader()).get("/api/projects/DevCockpit/roadmap")

    assert response.status_code == 502
    assert response.json()["source"] == {
        "status": "unavailable",
        "code": "GITHUB_RATE_LIMITED",
        "http_status": 403,
        "token_configured": True,
        "rate_limit_remaining": "0",
        "rate_limit_reset": "1791339999",
    }
    assert response.json()["pipeline"] is None


def test_unknown_project_is_local_404_without_github_call() -> None:
    response = client_for(FakeReader()).get("/api/projects/Unknown/roadmap")

    assert response.status_code == 404
