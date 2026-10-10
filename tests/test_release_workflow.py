"""DC-075B: targeted release/hotfix idempotency and safety regressions."""
from dataclasses import replace
import json

import httpx
import pytest

from app.domain.delivery_context import CompletionPolicy, DeliveryMode
from app.domain.project import Project
from app.infrastructure.github_release_workflow import (
    GitHubReleaseWorkflow, ReleaseWorkflowError,
)
from test_delivery_context import hotfix


def fixtures():
    ctx = hotfix()
    return ctx, Project("App", "owner/repo", 1, delivery_contexts=(ctx,))


def git_simulator(*, existing_branch=False, release_tip="a" * 40,
                  pr_base="release/1.4", wrong_fingerprint=False):
    ctx, _ = fixtures()
    branches = {"release/1.4": release_tip}
    if existing_branch:
        branches["dev/FIX-1"] = "b" * 40
    prs: list[dict] = []
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((request.method, path))
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 5, "full_name": "owner/repo"})
        if path == "/repos/owner/repo/issues/41":
            return httpx.Response(200, json={"body": "accepted"})
        prefix = "/repos/owner/repo/git/ref/heads/"
        if path.startswith(prefix):
            name = path.removeprefix(prefix)
            if name not in branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": "refs/heads/" + name,
                                             "object": {"type": "commit", "sha": branches[name]}})
        if path == "/repos/owner/repo/git/refs" and request.method == "POST":
            data = json.loads(request.content)
            name = data["ref"].removeprefix("refs/heads/")
            assert name != "main"
            if name in branches:
                return httpx.Response(422, json={"message": "already exists"})
            branches[name] = data["sha"]
            return httpx.Response(201, json={"ref": data["ref"]})
        if path.startswith("/repos/owner/repo/compare/"):
            start, end = path.rsplit("/", 1)[-1].split("...")
            if ((start == "0" * 40 and end == release_tip)
                    or (start == "a" * 40 and end in {release_tip, "b" * 40})):
                return httpx.Response(200, json={
                    "status": "ahead", "ahead_by": 1,
                    "merge_base_commit": {"sha": start},
                })
            return httpx.Response(200, json={"status": "diverged", "ahead_by": 1,
                                             "merge_base_commit": {"sha": "f" * 40}})
        if path == "/repos/owner/repo/pulls" and request.method == "GET":
            return httpx.Response(200, json=prs)
        if path == "/repos/owner/repo/pulls" and request.method == "POST":
            data = json.loads(request.content)
            assert data["base"] == "release/1.4"
            assert data["head"] == "dev/FIX-1"
            pr = {"number": 88, "state": "open", "title": data["title"],
                  "body": data["body"], "html_url": "https://github.com/owner/repo/pull/88",
                  "head": {"ref": data["head"], "sha": branches[data["head"]],
                           "repo": {"id": 5}},
                  "base": {"ref": pr_base}}
            prs.append(pr)
            return httpx.Response(201, json=pr)
        if path == "/repos/owner/repo/branches/release/1.4/protection":
            return httpx.Response(200, json={
                "required_status_checks": {"strict": True, "contexts": ["CI"]},
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            })
        if path.startswith("/repos/owner/repo/commits/") and path.endswith("/check-runs"):
            return httpx.Response(200, json={
                "total_count": 1, "check_runs": [
                    {"name": "CI", "status": "completed", "conclusion": "success"}
                ],
            })
        if path.startswith("/repos/owner/repo/commits/") and path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler), branches, prs, seen


def test_hotfix_branch_uses_only_pinned_release_not_current_main():
    ctx, project = fixtures()
    transport, branches, _, seen = git_simulator()
    service = GitHubReleaseWorkflow(transport=transport)
    assert service.prepare_hotfix(project, ctx) == "a" * 40
    assert service.prepare_hotfix(project, ctx) == "a" * 40
    assert branches["dev/FIX-1"] == "a" * 40
    assert [item for item in seen if item == ("POST", "/repos/owner/repo/git/refs")] == [
        ("POST", "/repos/owner/repo/git/refs")
    ]
    assert not any("/heads/main" in path for _, path in seen)


def test_moved_release_tip_blocks_hotfix_instead_of_falling_back_to_main():
    ctx, project = fixtures()
    transport, _, _, _ = git_simulator(release_tip="c" * 40)
    with pytest.raises(ReleaseWorkflowError, match="SOURCE_REF_MOVED"):
        GitHubReleaseWorkflow(transport=transport).prepare_hotfix(project, ctx)


def test_idempotent_pr_creation_targets_release_and_preserves_fingerprint():
    ctx, project = fixtures()
    transport, branches, prs, _ = git_simulator(existing_branch=True)
    service = GitHubReleaseWorkflow(transport=transport)
    first = service.ensure_hotfix_pr(project, ctx, title="FIX-1 — critical fix",
                                     expected_head_sha="b" * 40)
    again = service.ensure_hotfix_pr(project, ctx, title="FIX-1 — critical fix",
                                     expected_head_sha="b" * 40)
    assert first.number == 88 and first.created is True
    assert again.number == 88 and again.created is False
    assert len(prs) == 1 and prs[0]["base"]["ref"] == "release/1.4"
    assert "Delivery-Context-SHA256: " + ctx.fingerprint() in prs[0]["body"]
    assert branches["dev/FIX-1"] == "b" * 40


def test_retargeted_pr_is_rejected_even_when_sha_might_match():
    ctx, project = fixtures()
    transport, _, prs, _ = git_simulator(existing_branch=True, pr_base="main")
    service = GitHubReleaseWorkflow(transport=transport)
    service.ensure_hotfix_pr(project, ctx, title="FIX-1 — fix",
                             expected_head_sha="b" * 40)
    assert prs[0]["base"]["ref"] == "main"
    with pytest.raises(ReleaseWorkflowError, match="HOTFIX_PR_IDENTITY_MISMATCH"):
        service.ensure_hotfix_pr(project, ctx, title="FIX-1 — fix",
                                expected_head_sha="b" * 40)


def test_required_release_check_missing_is_not_green():
    transport, _, _, _ = git_simulator()
    service = GitHubReleaseWorkflow(transport=transport)
    with service._client() as client:
        required = service.require_release_protection(
            client, "https://api.github.com/repos/owner/repo", "release/1.4"
        )
        assert required == ("CI",)
        service.verify_release_checks(
            client, "https://api.github.com/repos/owner/repo", "b" * 40, required
        )
        with pytest.raises(ReleaseWorkflowError, match="REQUIRED_CHECK_NOT_GREEN"):
            service.verify_release_checks(
                client, "https://api.github.com/repos/owner/repo", "b" * 40,
                ("CI", "release-validation"),
            )

def test_merge_is_not_hotfix_completion_without_published_evidence():
    from app.domain.execution import (
        ExecutionEvidence, ExecutionState, NextAction,
        PullRequestEvidence, WorkflowRunEvidence, derive_execution_projection,
    )
    from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType
    item = WorkItem(key="FIX-1", type=WorkItemType.WORK,
                    status=WorkItemStatus.READY, parent="#1", lane="MAIN",
                    title="Maintained hotfix")
    pr = PullRequestEvidence(
        number=88, title="FIX-1 — fix", body="Work-Item: FIX-1",
        branch="dev/FIX-1", head_sha="b" * 40, state="closed",
        merged=True, mergeable=None, base_branch="release/1.4",
        merged_at="2026-10-10T16:00:00Z",
    )
    ci = (WorkflowRunEvidence(run_id=1, name="CI", status="completed",
                              conclusion="success", attempt=1, head_sha="b" * 40),)
    unvalidated = derive_execution_projection(
        item, ExecutionEvidence(default_branch="main", pull_requests=(pr,),
                                workflow_runs=ci, requires_verified_artifact=True)
    )
    assert unvalidated.state is ExecutionState.MERGED
    assert unvalidated.next_action is NextAction.WAIT
    validated = derive_execution_projection(
        item, ExecutionEvidence(default_branch="main", pull_requests=(pr,),
                                workflow_runs=ci, requires_verified_artifact=True,
                                artifact_verified=True)
    )
    assert validated.state is ExecutionState.ROADMAP_UPDATE_REQUIRED
    assert validated.next_action is NextAction.RECONCILE_ROADMAP


def test_hotfix_lock_serializes_publication_per_release():
    ctx, project = fixtures()
    first = project.resource_lock_requirements_for(ctx.work_item_id)
    assert [entry.surface.key for entry in first] == ["release:5:release/1.4"]
    assert first[0].mode.value == "EXCLUSIVE"
