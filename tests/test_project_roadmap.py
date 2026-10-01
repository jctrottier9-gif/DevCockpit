from app.application.roadmaps import RoadmapIssue, read_project_roadmap
from app.domain.project import Project


class FakeReader:
    def __init__(self, body: str) -> None:
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(project.repository_full_name, project.roadmap_issue_number, self.body)


def test_application_projection_uses_only_canonical_block() -> None:
    project = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
    body = """
Human prose says DC-999 is next.
<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
DC-020 | WORK | READY | #1 | MAIN | parser
DC-021 | WORK | BLOCKED | #1 | MAIN | execution
<!-- /COCKPIT_PIPELINE_V1 -->
"""

    projection = read_project_roadmap(project, reader=FakeReader(body))

    assert projection.pipeline.valid is True
    assert projection.pipeline.active_ready_item is not None
    assert projection.pipeline.active_ready_item.key == "DC-020"
