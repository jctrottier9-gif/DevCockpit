from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import PullRequestEvidence, WorkflowRunEvidence
from app.domain.flow_analytics import CommitEvidence, FlowAnalyticsEvidence, FlowDeliveryEvidence
from app.domain.project import Project
from app.main import create_app


P1 = Project("P1", "jctrottier9-gif/DevCockpit", 1)
P2 = Project("P2", "jctrottier9-gif/DevCockpit", 2)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
DC-061 | WORK | READY | #1 | MAIN | Flow Analytics | - | -
ARCH-1 | ARCHITECTURE_GATE | DONE | #1 | MAIN | Gate | - | -
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(project.repository_full_name, project.roadmap_issue_number, ROADMAP, "now")


class AnalyticsReader:
    def __init__(self):
        self.projects = []
        self.work_item_keys = []

    def read(self, project, work_items):
        self.projects.append(project.project_id)
        self.work_item_keys.append(tuple(item.key for item in work_items))
        work_item = work_items[0]
        return FlowAnalyticsEvidence((
            FlowDeliveryEvidence(
                work_item=work_item,
                pull_request=PullRequestEvidence(
                    number=35,
                    title="DC-061 — Flow Analytics",
                    body="Work-Item: DC-061",
                    branch="dc-061-flow-analytics",
                    head_sha="green",
                    state="closed",
                    merged=True,
                    mergeable=None,
                    url="https://github.example/pr/35",
                    created_at="2026-10-02T10:00:00Z",
                    updated_at="2026-10-02T11:00:00Z",
                    merged_at="2026-10-02T11:00:00Z",
                ),
                commits=(CommitEvidence("first", "2026-10-02T09:00:00Z"),),
                workflow_runs=(
                    WorkflowRunEvidence(
                        run_id=1,
                        name="CI",
                        status="completed",
                        conclusion="success",
                        attempt=1,
                        head_sha="green",
                        url="https://github.example/actions/1",
                        created_at="2026-10-02T10:15:00Z",
                        updated_at="2026-10-02T10:30:00Z",
                    ),
                ),
                changed_files=("app/main.py",),
            ),
        ))


def test_analytics_api_is_read_only_project_scoped_and_backend_calculated(tmp_path):
    reader = AnalyticsReader()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'api.db'}", execution_poll_seconds=0),
        project_catalog=ProjectCatalog([P1, P2]),
        roadmap_reader=RoadmapReader(),
        flow_analytics_reader=reader,
    )

    response = TestClient(app).get("/api/projects/P1/analytics")

    assert response.status_code == 200
    payload = response.json()
    assert reader.projects == ["P1"]
    assert reader.work_item_keys == [("DC-061",)]
    assert payload["project"]["project_id"] == "P1"
    assert payload["aggregates"]["delivery_count"] == 1
    assert payload["aggregates"]["median_commit_to_pr"] == {
        "seconds": 3600.0,
        "observations": 1,
    }
    assert payload["deliveries"][0]["durations"]["total_observable_duration_seconds"] == 7200.0
    assert payload["deliveries"][0]["ci"]["attempt_count"] == 1
    assert payload["deliveries"][0]["ci"]["recovered_after_red"] is False


def test_analytics_api_unknown_project_is_404_without_reader_call(tmp_path):
    reader = AnalyticsReader()
    app = create_app(
        Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'api.db'}", execution_poll_seconds=0),
        project_catalog=ProjectCatalog([P1]),
        roadmap_reader=RoadmapReader(),
        flow_analytics_reader=reader,
    )

    response = TestClient(app).get("/api/projects/Unknown/analytics")

    assert response.status_code == 404
    assert reader.projects == []
