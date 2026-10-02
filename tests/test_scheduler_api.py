from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.project import Project
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | DONE | #1 | MAIN | A | - | -
B | WORK | READY | #1 | MAIN | B | - | A
C | WORK | BLOCKED | #1 | MAIN | C | - | B
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(project.repository_full_name, 1, ROADMAP, "now")


def test_scheduler_get_is_read_only_explainable_projection(tmp_path):
    app = create_app(
        Settings(
            execution_poll_seconds=0,
            database_url=f"sqlite+pysqlite:///{tmp_path / 'scheduler.db'}",
        ),
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=RoadmapReader(),
    )
    client = TestClient(app)

    response = client.get("/api/projects/DevCockpit/scheduler")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scheduler"]["valid"] is True
    assert payload["scheduler"]["pipeline_version"] == 3
    assert payload["scheduler"]["executable_candidates"] == ["B"]
    items = {item["key"]: item for item in payload["scheduler"]["work_items"]}
    assert items["B"]["dependencies"] == ["A"]
    assert items["B"]["executable"] is True
    assert items["B"]["next_action"] == "START_DEV"
    assert items["C"]["unsatisfied_dependencies"] == ["B"]
    assert items["C"]["reason"] == "WAITING_FOR_DEPENDENCY"
