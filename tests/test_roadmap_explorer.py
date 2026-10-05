from __future__ import annotations

from fastapi.testclient import TestClient

from app.application.projects import ProjectCatalog
from app.application.roadmap_explorer import (
    RoadmapExplorerIssueDetail,
    parse_documented_issue_mappings,
    read_project_roadmap_explorer,
)
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.project import Project
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """# Roadmap

## Issues de livraison

| Clé | Issue |
|---|---:|
| DONE | #10 |
| NOW | #11 |
| PAR | #12 |
| NEXT | #13 |
| LATER | #14 |

## Pipeline canonique

<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
DONE | WORK | DONE | #41 | MAIN | Delivered foundation | - | -
NOW | WORK | READY | #41 | MAIN | Current delivery | - | DONE
PAR | WORK | READY | #41 | PARALLEL | Parallel delivery | - | DONE
NEXT | WORK | BLOCKED | #41 | MAIN | Next delivery | - | NOW
LATER | WORK | BLOCKED | #41 | MAIN | Later delivery | - | NEXT
<!-- /COCKPIT_PIPELINE_V3 -->
"""


class RoadmapReader:
    def __init__(self, body: str = ROADMAP):
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            "2026-10-05T00:00:00Z",
        )


class EvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


class IssueReader:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def read(self, repository_full_name: str, issue_number: int) -> RoadmapExplorerIssueDetail:
        self.calls.append((repository_full_name, issue_number))
        return RoadmapExplorerIssueDetail(
            repository_full_name=repository_full_name,
            number=issue_number,
            title=f"Issue {issue_number}",
            body=f"Body for {issue_number}",
            state="open",
            url=f"https://github.com/{repository_full_name}/issues/{issue_number}",
            updated_at="2026-10-05T00:00:00Z",
        )


def application(tmp_path, *, roadmap_reader=None, issue_reader=None):
    settings = Settings(
        execution_poll_seconds=0,
        max_parallel_dev_executions=2,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'roadmap-explorer.db'}",
    )
    upgrade_database(settings)
    return create_app(
        settings,
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=roadmap_reader or RoadmapReader(),
        execution_reader=EvidenceReader(),
        github_issue_reader=issue_reader or IssueReader(),
    )


def test_issue_mapping_parser_is_bounded_to_delivery_section():
    body = """| OUTSIDE | #999 |

## Issues de livraison

| Clé | Issue |
|---|---:|
| A | #12 |
| B | [#13](https://github.com/acme/repo/issues/13) |

## Other

| C | #14 |
"""
    mappings, diagnostics = parse_documented_issue_mappings(body)

    assert mappings == {"A": 12, "B": 13}
    assert diagnostics == ()


def test_duplicate_and_conflicting_issue_mappings_are_diagnostic_and_not_resolved():
    body = """## Issues de livraison

| Clé | Issue |
|---|---:|
| DUP | #12 |
| DUP | #12 |
| CONFLICT | #13 |
| CONFLICT | #14 |
"""
    mappings, diagnostics = parse_documented_issue_mappings(body)

    assert mappings == {}
    assert {item.code for item in diagnostics} == {
        "DUPLICATE_ISSUE_MAPPING",
        "CONFLICTING_ISSUE_MAPPING",
    }


def test_projection_exposes_horizons_dependencies_and_distinct_work_parent_issues():
    projection = read_project_roadmap_explorer(
        PROJECT,
        roadmap_reader=RoadmapReader(),
    )
    by_key = {item.key: item for item in projection.items}

    assert projection.pipeline_valid
    assert projection.pipeline_version == 3
    assert projection.now == "NOW"
    assert projection.parallel == ("PAR",)
    assert projection.next == "NEXT"
    assert projection.later == ("LATER",)
    assert projection.history == ("DONE",)

    now = by_key["NOW"]
    assert now.work_issue is not None
    assert now.work_issue.number == 11
    assert now.parent_issue.number == 41
    assert now.depends_on == ("DONE",)
    assert now.unsatisfied_dependencies == ()
    assert now.executable


def test_missing_or_ambiguous_issue_mapping_never_changes_scheduler_eligibility():
    body = ROADMAP.replace("| NOW | #11 |\n", "")
    projection = read_project_roadmap_explorer(
        PROJECT,
        roadmap_reader=RoadmapReader(body),
    )
    now = next(item for item in projection.items if item.key == "NOW")

    assert now.work_issue is None
    assert now.executable
    assert now.scheduler_state == "EXECUTABLE"


def test_api_loads_issue_detail_only_on_demand_and_rejects_unreferenced_issue(tmp_path):
    issue_reader = IssueReader()
    app = application(tmp_path, issue_reader=issue_reader)
    client = TestClient(app)

    explorer = client.get("/api/projects/DevCockpit/roadmap-explorer")
    assert explorer.status_code == 200
    assert explorer.json()["horizons"] == {
        "now": "NOW",
        "parallel": ["PAR"],
        "next": "NEXT",
        "later": ["LATER"],
        "history": ["DONE"],
    }
    assert issue_reader.calls == []

    work_issue = client.get("/api/projects/DevCockpit/roadmap-explorer/issues/11")
    assert work_issue.status_code == 200
    assert work_issue.json()["title"] == "Issue 11"
    assert issue_reader.calls == [(PROJECT.repository_full_name, 11)]

    parent_issue = client.get("/api/projects/DevCockpit/roadmap-explorer/issues/41")
    assert parent_issue.status_code == 200
    assert issue_reader.calls[-1] == (PROJECT.repository_full_name, 41)

    unreferenced = client.get("/api/projects/DevCockpit/roadmap-explorer/issues/999")
    assert unreferenced.status_code == 404
    assert issue_reader.calls == [
        (PROJECT.repository_full_name, 11),
        (PROJECT.repository_full_name, 41),
    ]

def test_invalid_pipeline_is_fail_closed_and_exposes_diagnostics():
    body = ROADMAP.replace(
        "NEXT | WORK | BLOCKED | #41 | MAIN | Next delivery | - | NOW",
        "NEXT | WORK | READY | #41 | MAIN | Next delivery | - | NOW",
    )
    projection = read_project_roadmap_explorer(
        PROJECT,
        roadmap_reader=RoadmapReader(body),
    )

    assert not projection.pipeline_valid
    assert not projection.scheduler_valid
    assert projection.now is None
    assert projection.parallel == ()
    assert projection.next is None
    assert projection.later == ()
    assert "MULTIPLE_MAIN_READY" in {
        diagnostic.code for diagnostic in projection.pipeline_diagnostics
    }

