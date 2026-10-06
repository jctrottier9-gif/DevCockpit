from __future__ import annotations

from fastapi.testclient import TestClient

from app.application.cockpit_panels import (
    ArchitectureDocumentDetail,
    ArchitectureDocumentReference,
    ReviewEvidence,
    ReviewJobEvidence,
    ReviewPullRequestEvidence,
    ReviewWorkflowEvidence,
)
from app.application.projects import ProjectCatalog
from app.application.prompt_dispatches import (
    CreatePromptDispatchCommand,
    create_prompt_dispatch,
)
from app.application.roadmap_explorer import RoadmapExplorerIssueDetail
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.project import Project
from app.domain.prompt_dispatch import PromptDispatchRole
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ARCH_ROADMAP = """## Issues de livraison

| WorkItem | Issue |
| --- | --- |
| ASTRA-TEST | #42 |
| NEXT | #43 |

<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
PREV | WORK | DONE | #41 | MAIN | Previous | - | -
ASTRA-TEST | ARCHITECTURE_GATE | READY | #41 | MAIN | Architecture decision | - | PREV
NEXT | WORK | BLOCKED | #41 | MAIN | Next work | - | ASTRA-TEST
<!-- /COCKPIT_PIPELINE_V3 -->"""
REVIEW_ROADMAP = """## Issues de livraison

| WorkItem | Issue |
| --- | --- |
| MAIN-A | #51 |
| PAR-B | #52 |

<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
PREV | WORK | DONE | #41 | MAIN | Previous | - | -
MAIN-A | WORK | READY | #41 | MAIN | Main delivery | - | PREV
PAR-B | WORK | READY | #41 | PARALLEL | Parallel delivery | - | PREV
NEXT | WORK | BLOCKED | #41 | MAIN | Later | - | MAIN-A
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def __init__(self, body: str):
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            "2026-10-05T01:00:00Z",
        )


class EvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


class IssueReader:
    def read(self, repository_full_name: str, issue_number: int) -> RoadmapExplorerIssueDetail:
        body = (
            "Architecture gate decision. Applicable reference: ADR-0015."
            if issue_number == 42
            else "Parent initiative context."
        )
        return RoadmapExplorerIssueDetail(
            repository_full_name=repository_full_name,
            number=issue_number,
            title=f"Issue {issue_number}",
            body=body,
            state="open",
            url=f"https://github.example/issues/{issue_number}",
            updated_at="2026-10-05T01:00:00Z",
        )


class DocumentReader:
    def __init__(self):
        self.read_calls = 0

    def list(self, repository_full_name: str):
        return (
            ArchitectureDocumentReference(
                adr_id="ADR-0015",
                title="hybrid cockpit and projection contracts",
                path="docs/architecture/ADR-0015-hybrid-cockpit-and-projection-contracts.md",
                url="https://github.example/ADR-0015",
            ),
        )

    def read(self, repository_full_name: str, path: str):
        self.read_calls += 1
        reference = self.list(repository_full_name)[0]
        return ArchitectureDocumentDetail(reference=reference, content="# ADR-0015\nDecision")


class EmptyReviewReader:
    def read(self, project, work_items):
        return ReviewEvidence(())


class CapturingReviewReader:
    def __init__(self):
        self.work_items = ()

    def read(self, project, work_items):
        self.work_items = tuple(item.key for item in work_items)
        red = ReviewWorkflowEvidence(
            run_id=100,
            name="CI",
            status="completed",
            conclusion="failure",
            attempt=2,
            head_sha="sha-main",
            url="https://github.example/actions/100",
            jobs=(
                ReviewJobEvidence(
                    job_id=1,
                    name="backend",
                    status="completed",
                    conclusion="failure",
                    url="https://github.example/jobs/1",
                    started_at=None,
                    completed_at=None,
                ),
            ),
            jobs_complete=True,
        )
        running = ReviewWorkflowEvidence(
            run_id=200,
            name="CI",
            status="in_progress",
            conclusion=None,
            attempt=1,
            head_sha="sha-par",
            url="https://github.example/actions/200",
            jobs=(
                ReviewJobEvidence(
                    job_id=2,
                    name="frontend",
                    status="in_progress",
                    conclusion=None,
                    url="https://github.example/jobs/2",
                    started_at=None,
                    completed_at=None,
                ),
            ),
            jobs_complete=True,
        )
        return ReviewEvidence(
            (
                ReviewPullRequestEvidence(
                    work_item_id="MAIN-A",
                    work_item_title="Main delivery",
                    lane="MAIN",
                    number=10,
                    title="MAIN-A delivery",
                    branch="main-a-work",
                    head_sha="sha-main",
                    url="https://github.example/pr/10",
                    mergeable=False,
                    auto_merge_enabled=True,
                    base_branch="main",
                    base_sha="base-main",
                    behind_by=0,
                    finalization_state=None,
                    finalization_detail=None,
                    ci_state="RED",
                    workflows=(red,),
                ),
                ReviewPullRequestEvidence(
                    work_item_id="PAR-B",
                    work_item_title="Parallel delivery",
                    lane="PARALLEL",
                    number=11,
                    title="PAR-B delivery",
                    branch="par-b-work",
                    head_sha="sha-par",
                    url="https://github.example/pr/11",
                    mergeable=None,
                    auto_merge_enabled=False,
                    base_branch="main",
                    mergeable_state="behind",
                    base_sha="base-par",
                    behind_by=1,
                    finalization_state=None,
                    finalization_detail=None,
                    ci_state="RUNNING",
                    workflows=(running,),
                ),
            )
        )


def application(tmp_path, *, roadmap_body: str, document_reader=None, review_reader=None):
    settings = Settings(
        execution_poll_seconds=0,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'dc070d.db'}",
    )
    upgrade_database(settings)
    return create_app(
        settings,
        project_catalog=ProjectCatalog([PROJECT]),
        roadmap_reader=RoadmapReader(roadmap_body),
        execution_reader=EvidenceReader(),
        github_issue_reader=IssueReader(),
        architecture_document_reader=document_reader or DocumentReader(),
        review_reader=review_reader or EmptyReviewReader(),
    )


def test_architecture_panel_is_read_only_and_correlates_only_gate_authorization(tmp_path):
    documents = DocumentReader()
    app = application(
        tmp_path,
        roadmap_body=ARCH_ROADMAP,
        document_reader=documents,
    )
    client = TestClient(app)

    generic = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id=PROJECT.project_id,
            work_item_id="ASTRA-TEST",
            role=PromptDispatchRole.ARCH,
            prompt_text="Generic architecture consultation",
            idempotency_key="generic-architecture-consultation",
        ),
        uow_factory=app.state.uow_factory,
    )

    before = client.get("/api/projects/DevCockpit/architecture-panel")
    assert before.status_code == 200
    gate = before.json()["gates"][0]
    assert gate["work_item_id"] == "ASTRA-TEST"
    assert gate["human_authorization_required"] is True
    assert gate["can_authorize"] is True
    assert gate["authorization"] is None
    assert [item["adr_id"] for item in gate["adrs"]] == ["ADR-0015"]

    with app.state.uow_factory() as uow:
        dispatches = uow.prompt_dispatches.list_for_work_item("DevCockpit", "ASTRA-TEST")
    assert [item.dispatch_id for item in dispatches] == [generic.dispatch_id]

    authorized = client.post(
        "/api/projects/DevCockpit/architecture-gates/ASTRA-TEST/authorize",
        json={"confirm": True},
    )
    assert authorized.status_code == 200

    after = client.get("/api/projects/DevCockpit/architecture-panel")
    gate = after.json()["gates"][0]
    assert gate["human_authorization_required"] is False
    assert gate["can_authorize"] is False
    assert gate["authorization"]["dispatch_id"] == authorized.json()["dispatch_id"]
    assert gate["authorization"]["agent_session"] == "DevCockpit:ARCH:ASTRA-TEST"

    detail = client.get(
        "/api/projects/DevCockpit/architecture-panel/gates/ASTRA-TEST/adrs/ADR-0015"
    )
    assert detail.status_code == 200
    assert detail.json()["reference"]["adr_id"] == "ADR-0015"
    assert "# ADR-0015" in detail.json()["content"]
    assert documents.read_calls == 1

    missing = client.get(
        "/api/projects/DevCockpit/architecture-panel/gates/ASTRA-TEST/adrs/ADR-0005"
    )
    assert missing.status_code == 404


def test_review_panel_preserves_work_item_pr_head_run_attempt_and_jobs(tmp_path):
    reviews = CapturingReviewReader()
    app = application(
        tmp_path,
        roadmap_body=REVIEW_ROADMAP,
        review_reader=reviews,
    )

    response = TestClient(app).get("/api/projects/DevCockpit/review-panel")

    assert response.status_code == 200
    payload = response.json()
    assert reviews.work_items == ("MAIN-A", "PAR-B")
    assert payload["complete"] is True
    assert [
        (item["work_item_id"], item["number"], item["head_sha"], item["ci_state"])
        for item in payload["pull_requests"]
    ] == [
        ("MAIN-A", 10, "sha-main", "RED"),
        ("PAR-B", 11, "sha-par", "RUNNING"),
    ]
    workflow = payload["pull_requests"][0]["workflows"][0]
    assert (workflow["run_id"], workflow["attempt"], workflow["head_sha"]) == (
        100,
        2,
        "sha-main",
    )
    assert workflow["jobs"][0]["name"] == "backend"
    assert payload["pull_requests"][0]["auto_merge_enabled"] is True
    assert payload["pull_requests"][1]["base_sha"] == "base-par"
    assert payload["pull_requests"][1]["mergeable_state"] == "behind"
    assert payload["pull_requests"][1]["next_action"] is None
