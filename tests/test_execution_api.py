from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence, PullRequestEvidence, WorkflowRunEvidence
from app.domain.project import Project
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
DC-021 | WORK | READY | #1 | MAIN | projection exécution et suivi CI
DC-030 | WORK | BLOCKED | #1 | MAIN | retour explicite réponse ChatGPT
<!-- /COCKPIT_PIPELINE_V1 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(project.repository_full_name, 1, ROADMAP, "now")


class ExecutionReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(
            default_branch="main",
            pull_requests=(
                PullRequestEvidence(
                    number=22,
                    title="DC-021 — execution projection",
                    body="",
                    branch="dc-021-execution-ci",
                    head_sha="abc123",
                    state="open",
                    merged=False,
                    mergeable=False,
                    url="https://github.example/pr/22",
                ),
            ),
            workflow_runs=(
                WorkflowRunEvidence(
                    run_id=123,
                    name="CI",
                    status="completed",
                    conclusion="failure",
                    attempt=1,
                    head_sha="abc123",
                    failed_jobs=("backend / pytest",),
                ),
            ),
        )


def test_execution_get_is_read_only_projection_surface() -> None:
    app = create_app(
        Settings(execution_poll_seconds=0),
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=RoadmapReader(),
        execution_reader=ExecutionReader(),
    )
    client = TestClient(app)

    response = client.get("/api/projects/DevCockpit/execution")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["project_id"] == "DevCockpit"
    assert payload["work_item"]["key"] == "DC-021"
    assert payload["execution_state"] == "CI_RED"
    assert payload["next_action"] == "FIX_CI"
    assert payload["pull_request"]["number"] == 22
    assert payload["head_sha"] == "abc123"
    assert payload["ci"]["failed_jobs"] == ["backend / pytest"]
    assert "prompt_dispatch" not in payload
