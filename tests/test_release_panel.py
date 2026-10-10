"""DC-075D acceptance: independent hotfix and forward-port evidence, no UI authority."""
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.release_panel import build_release_panel_router
from app.application.release_panel import read_project_release_panel
from app.application.roadmaps import RoadmapIssue
from app.application.projects import ProjectCatalog
from app.application.executions import ExecutionSourceError
from app.domain.delivery_context import (
    CompletionPolicy, DeliveryContext, DeliveryMode, ReferenceKind,
)
from app.domain.execution import (
    ExecutionEvidence, PullRequestEvidence, WorkflowRunEvidence,
)
from app.domain.project import Project


MAIN_SHA = "1" * 40
RELEASE_ORIGIN = "2" * 40
RELEASE_BASE = "3" * 40
INTEGRATED = "4" * 40
HOTFIX_HEAD = "5" * 40
FWD_HEAD = "6" * 40
FWD_MERGE = "7" * 40

ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
FIX-42 | WORK | DONE | #132 | MAIN | Fix maintained release | - | -
FIX-42-FWD | WORK | READY | #132 | MAIN | Forward port fix | - | FIX-42
<!-- /COCKPIT_PIPELINE_V3 -->"""


def contexts():
    shared = dict(
        schema_version=1, repository_id=12345,
        repository_full_name="acme/system", accepted_issue_body_sha256="a" * 64,
        ref_kind=ReferenceKind.BRANCH,
    )
    hotfix = DeliveryContext(
        **shared, work_item_id="FIX-42", delivery_issue_number=20,
        mode=DeliveryMode.HOTFIX,
        requested_ref="release/1.4", resolved_ref="refs/heads/release/1.4",
        source_sha=RELEASE_BASE,
        expected_pr_base="release/1.4", observed_pr_base_sha=RELEASE_BASE,
        expected_work_branch="hotfix/fix-42", starting_sha=RELEASE_BASE,
        correction_id="FIX-42", linked_work_item="FIX-42-FWD",
        release_id="1.4", release_branch="release/1.4",
        release_origin_sha=RELEASE_ORIGIN, release_state="MAINTAINED",
        completion_policy=CompletionPolicy.VERIFIED_ARTIFACT,
    )
    forward = DeliveryContext(
        **shared, work_item_id="FIX-42-FWD", delivery_issue_number=21,
        mode=DeliveryMode.FORWARD_PORT,
        requested_ref="main", resolved_ref="refs/heads/main", source_sha=MAIN_SHA,
        expected_pr_base="main", observed_pr_base_sha=MAIN_SHA,
        expected_work_branch="forward-port/fix-42", starting_sha=MAIN_SHA,
        correction_id="FIX-42", linked_work_item="FIX-42",
        source_hotfix_pr=50, integrated_commits=(INTEGRATED,),
        forward_port_method="SQUASH",
    )
    return hotfix, forward


class RoadmapReader:
    body = ROADMAP

    def read(self, project):
        return RoadmapIssue(project.repository_full_name, project.roadmap_issue_number, self.body)


class AcceptanceUow:
    def __init__(self, values):
        self.values = values
        self.delivery_contexts = self

    def get(self, repository_id, work_item_id):
        return self.values.get((repository_id, work_item_id))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class EvidenceReader:
    def __init__(self):
        self.values = {}
        self.calls = []

    def read(self, project, item):
        self.calls.append(item.key)
        value = self.values.get(item.key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return ExecutionEvidence(default_branch="main")
        return value


class ArtifactReader:
    def __init__(self):
        self.published = False
        self.calls = []

    def find_published_artifact(self, project, context, *, integrated_sha):
        self.calls.append((context.work_item_id, integrated_sha))
        if not self.published:
            return None
        return SimpleNamespace(
            tag="v1.4.1", source_sha=INTEGRATED,
            image="registry.example/app@sha256:" + "b" * 64,
            digest="sha256:" + "b" * 64, validation_check="release-ci",
        )


def pr(context, *, number, head, merged, merge_sha=None, target=None):
    return PullRequestEvidence(
        number=number, title=context.work_item_id + " delivery",
        body="Work-Item: " + context.work_item_id
             + "\nDelivery-Context-SHA256: " + context.fingerprint(),
        branch=context.expected_work_branch, head_sha=head,
        state="closed" if merged else "open", merged=merged, mergeable=True,
        base_branch=target or context.expected_pr_base,
        base_sha=context.observed_pr_base_sha,
        merge_commit_sha=merge_sha,
        url=f"https://github.com/acme/system/pull/{number}",
    )


def observed(context, *, number, head, merged, merge_sha=None, ci="GREEN",
             artifact_verified=False, target=None):
    run = WorkflowRunEvidence(
        run_id=number * 10, name="CI",
        status="in_progress" if ci == "RUNNING" else "completed",
        conclusion=None if ci == "RUNNING" else "failure" if ci == "RED" else "success",
        attempt=1, head_sha=head,
        url=f"https://github.com/acme/system/actions/runs/{number * 10}",
    )
    return ExecutionEvidence(
        default_branch="main",
        pull_requests=(pr(context, number=number, head=head, merged=merged,
                          merge_sha=merge_sha, target=target),),
        workflow_runs=(run,),
        requires_verified_artifact=context.mode is DeliveryMode.HOTFIX,
        artifact_verified=artifact_verified,
    )


def setup():
    hotfix, forward = contexts()
    project = Project("System", "acme/system", 1, delivery_contexts=(hotfix, forward))
    reader, artifact = EvidenceReader(), ArtifactReader()
    persisted = {(c.repository_id, c.work_item_id): c for c in (hotfix, forward)}
    def uow_factory():
        return AcceptanceUow(persisted)
    def read():
        return read_project_release_panel(
            project, roadmap_reader=RoadmapReader(),
            evidence_reader=reader, artifact_reader=artifact, uow_factory=uow_factory,
        )
    return project, hotfix, forward, reader, artifact, persisted, uow_factory, read


def test_release_to_main_acceptance_stages_are_independent():
    project, hotfix, forward, reader, artifact, persisted, uow, read = setup()

    # Version N is frozen and main has evolved. A hotfix PR must still be
    # associated with the release, not the moving default branch.
    reader.values[hotfix.work_item_id] = observed(
        hotfix, number=50, head=HOTFIX_HEAD, merged=False, ci="RUNNING",
    )
    first = {row["work_item_id"]: row for row in read()["items"]}
    assert first["FIX-42"]["delivery_state"] == "CI_RUNNING"
    assert first["FIX-42"]["pr"]["base_branch"] == "release/1.4"
    assert first["FIX-42"]["source_sha"] == RELEASE_BASE
    assert first["FIX-42"]["release_origin_sha"] == RELEASE_ORIGIN
    assert first["FIX-42-FWD"]["target_branch"] == "main"
    assert first["FIX-42-FWD"]["source_sha"] == MAIN_SHA
    assert first["FIX-42"]["deployment"] == "NOT_VERIFIED"

    # Merge into release is not publication, validation or forward-port.
    reader.values["FIX-42"] = observed(
        hotfix, number=50, head=HOTFIX_HEAD, merged=True, merge_sha=INTEGRATED,
    )
    merged = {row["work_item_id"]: row for row in read()["items"]}
    assert merged["FIX-42"]["delivery_state"] == "MERGED"
    assert merged["FIX-42"]["artifact"]["status"] == "PENDING"
    assert merged["FIX-42-FWD"]["pr"] is None
    assert merged["FIX-42-FWD"]["delivery_state"] == "READY"

    # Only verified version/tag/digest evidence can become a publication
    # proof. The independent forward-port PR can still have red CI.
    artifact.published = True
    reader.values["FIX-42"] = observed(
        hotfix, number=50, head=HOTFIX_HEAD, merged=True,
        merge_sha=INTEGRATED, artifact_verified=True,
    )
    reader.values["FIX-42-FWD"] = observed(
        forward, number=51, head=FWD_HEAD, merged=False, ci="RED",
    )
    tested = {row["work_item_id"]: row for row in read()["items"]}
    assert tested["FIX-42"]["artifact"]["tag"] == "v1.4.1"
    assert tested["FIX-42"]["artifact"]["digest"] == "sha256:" + "b" * 64
    assert tested["FIX-42-FWD"]["delivery_state"] == "CI_RED"
    assert tested["FIX-42-FWD"]["ci_state"] == "RED"
    assert tested["FIX-42-FWD"]["integrated_commits"] == [INTEGRATED]
    assert tested["FIX-42-FWD"]["source_hotfix_pr"] == 50
    assert all(row["sql_compatibility"] == "NOT_ATTESTED" for row in tested.values())

    # Independently merged on main: two GitHub PRs and two distinct bases.
    reader.values["FIX-42-FWD"] = observed(
        forward, number=51, head=FWD_HEAD, merged=True, merge_sha=FWD_MERGE,
    )
    final = {row["work_item_id"]: row for row in read()["items"]}
    assert final["FIX-42-FWD"]["delivery_state"] == "MERGED"
    assert final["FIX-42"]["pr"]["number"] != final["FIX-42-FWD"]["pr"]["number"]
    assert final["FIX-42-FWD"]["artifact"]["status"] == "NOT_APPLICABLE"
    assert final["FIX-42-FWD"]["deployment"] == "NOT_VERIFIED"


def test_wrong_pr_target_blocked_even_when_target_shas_are_identical():
    _, hotfix, forward, reader, _, _, _, read = setup()
    reader.values["FIX-42"] = observed(
        hotfix, number=50, head=HOTFIX_HEAD, merged=False, target="main",
    )
    row = read()["items"][0]
    assert row["delivery_state"] == "BLOCKED"
    assert "PR_TARGET_OR_CONTEXT_MISMATCH" in row["diagnostics"]
    assert row["pr"] is None


def test_unaccepted_or_missing_evidence_cannot_look_delivered():
    _, hotfix, forward, reader, artifact, persisted, _, read = setup()
    persisted.pop((hotfix.repository_id, hotfix.work_item_id))
    rows = {row["work_item_id"]: row for row in read()["items"]}
    assert rows["FIX-42"]["delivery_state"] == "BLOCKED"
    assert rows["FIX-42-FWD"]["delivery_state"] == "BLOCKED"
    assert reader.calls == []

    persisted[(hotfix.repository_id, hotfix.work_item_id)] = hotfix
    reader.values["FIX-42"] = ExecutionSourceError("Moved tag, missing required CI or GitHub unavailable")
    rows = {row["work_item_id"]: row for row in read()["items"]}
    assert rows["FIX-42"]["delivery_state"] == "BLOCKED"
    assert rows["FIX-42"]["ci_state"] == "UNKNOWN"
    assert rows["FIX-42"]["artifact"]["status"] == "NOT_VERIFIED"
    assert rows["FIX-42"]["deployment"] == "NOT_VERIFIED"


def test_read_only_route_rehydrates_after_restart_and_is_project_scoped():
    project, hotfix, forward, reader, artifact, persisted, uow_factory, _ = setup()
    reader.values["FIX-42"] = observed(
        hotfix, number=50, head=HOTFIX_HEAD, merged=True, merge_sha=INTEGRATED,
    )
    app = FastAPI()
    app.include_router(build_release_panel_router(
        project_catalog=ProjectCatalog([project]),
        roadmap_reader=RoadmapReader(), evidence_reader=reader,
        artifact_reader=artifact, uow_factory=uow_factory,
    ))
    client = TestClient(app)
    url = "/api/projects/System/release-panel"
    before, after = client.get(url), client.get(url)
    assert before.status_code == after.status_code == 200
    assert before.json()["items"] == after.json()["items"]
    assert len(before.json()["items"]) == 2
    assert client.get("/api/projects/Other/release-panel").status_code == 404


def test_invalid_roadmap_blocks_all_git_evidence_reads():
    project, _, _, reader, artifact, persisted, uow_factory, _ = setup()
    invalid = RoadmapReader()
    invalid.body = "No canonical roadmap block"
    response = read_project_release_panel(
        project, roadmap_reader=invalid, evidence_reader=reader,
        artifact_reader=artifact, uow_factory=uow_factory,
    )
    assert response["source"]["status"] == "unavailable"
    assert response["items"] == []
    assert reader.calls == []


def test_structured_work_item_prevents_hotfix_forward_port_pr_aliasing():
    from app.domain.execution import pull_request_matches_work_item
    hotfix, forward = contexts()
    forward_pr = pr(forward, number=51, head=FWD_HEAD, merged=False)
    assert pull_request_matches_work_item(forward_pr, forward.work_item_id)
    assert not pull_request_matches_work_item(forward_pr, hotfix.work_item_id)
    assert not pull_request_matches_work_item(
        replace(forward_pr, body=forward_pr.body + "\nWork-Item: FIX-42"),
        forward.work_item_id,
    )
