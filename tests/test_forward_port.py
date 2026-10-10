"""DC-075C: source integration, independent main branch and PR safety."""
from dataclasses import replace
import json

import httpx
import pytest

from app.domain.project import Project
from app.infrastructure.github_forward_port import (
    ForwardPortError, GitHubForwardPortWorkflow,
)
from app.infrastructure.github_release_workflow import ReleaseWorkflowError
from test_delivery_context import forward_port, hotfix


A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


def simulator(*, kind="squash", already_present=False, missing_provenance=False,
              retargeted=False, moved_main=False):
    hot = hotfix()
    fwd = replace(forward_port(), integrated_commits=(C,),
                  expected_work_branch="forward/FWD-1")
    if kind == "merge":
        fwd = replace(fwd, integrated_commits=(C,),
                      forward_port_method="MERGE_DELTA_ADAPT")
    if kind == "rebase":
        fwd = replace(fwd, integrated_commits=(D, C),
                      forward_port_method="CHERRY_PICK_REBASE")
    if already_present:
        fwd = replace(fwd, source_sha=C, starting_sha=C,
                      observed_pr_base_sha=C)
    project = Project("App", "owner/repo", 1, delivery_contexts=(hot, fwd))
    branches = {
        "main": C if already_present else B if moved_main else A,
        "release/1.4": C,
    }
    prs = []
    seen = []
    parents = {
        A: [],
        B: [{"sha": A}],
        C: [{"sha": A}, {"sha": D}] if kind == "merge" else
           [{"sha": D}] if kind == "rebase" else [{"sha": A}],
        D: [{"sha": A}],
    }
    messages = ["Fix only\n\n(cherry picked from commit " + sha + ")"
                for sha in fwd.integrated_commits]
    if missing_provenance:
        messages = ["Unrelated feature"]

    def handler(request):
        path = request.url.path
        method = request.method
        seen.append((method, path))
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 5, "full_name": "owner/repo"})
        if path == "/repos/owner/repo/issues/41":
            return httpx.Response(200, json={"body": "accepted"})
        prefix = "/repos/owner/repo/git/ref/heads/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            if name not in branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={
                "ref": "refs/heads/" + name,
                "object": {"type": "commit", "sha": branches[name]},
            })
        if path == "/repos/owner/repo/git/refs" and method == "POST":
            data = json.loads(request.content)
            name = data["ref"].removeprefix("refs/heads/")
            if name in branches:
                return httpx.Response(422)
            branches[name] = data["sha"]
            return httpx.Response(201, json={"ref": data["ref"]})
        if path.startswith("/repos/owner/repo/commits/"):
            sha = path.rsplit("/", 1)[-1]
            if sha in parents:
                return httpx.Response(200, json={"sha": sha, "parents": parents[sha]})
        if path == "/repos/owner/repo/pulls/123":
            return httpx.Response(200, json={
                "number": 123, "merged_at": "2026-10-10T16:00:00Z",
                "merge_commit_sha": C,
                "head": {"ref": "dev/FIX-1", "repo": {"id": 5}},
                "base": {"ref": "release/1.4"},
                "body": "\n".join([
                    "Work-Item: FIX-1",
                    "Correction-Id: CORR-1",
                    "Delivery-Context-SHA256: " + hot.fingerprint(),
                ]),
            })
        if path.startswith("/repos/owner/repo/compare/"):
            start, end = path.rsplit("/", 1)[-1].split("...")
            if start == A and end == C:
                return httpx.Response(200, json={
                    "status": "ahead", "ahead_by": 2,
                    "merge_base_commit": {"sha": A},
                })
            if start in {C, D} and end == A:
                return httpx.Response(200, json={
                    "status": "diverged", "ahead_by": 0, "behind_by": 1,
                    "merge_base_commit": {"sha": A},
                })
            if start == B and end == A:
                return httpx.Response(200, json={
                    "status": "diverged", "ahead_by": 0,
                    "merge_base_commit": {"sha": A},
                })
            if start == A and end == D:
                return httpx.Response(200, json={
                    "status": "ahead", "ahead_by": 1,
                    "merge_base_commit": {"sha": A},
                })
            if start == A and end == B:
                return httpx.Response(200, json={
                    "status": "ahead", "ahead_by": 1, "behind_by": 0,
                    "total_commits": len(messages),
                    "commits": [
                        {"sha": B, "commit": {"message": m}} for m in messages
                    ], "merge_base_commit": {"sha": A},
                })
            return httpx.Response(200, json={
                "status": "diverged", "ahead_by": 0,
                "merge_base_commit": {"sha": A},
            })
        if path == "/repos/owner/repo/pulls" and method == "GET":
            return httpx.Response(200, json=prs)
        if path == "/repos/owner/repo/pulls" and method == "POST":
            data = json.loads(request.content)
            pr = {
                "number": 150, "title": data["title"], "body": data["body"],
                "html_url": "https://github.com/owner/repo/pull/150",
                "head": {"ref": data["head"], "sha": branches[data["head"]],
                         "repo": {"id": 5}},
                "base": {"ref": "release/1.4" if retargeted else data["base"]},
            }
            prs.append(pr)
            return httpx.Response(201, json=pr)
        if path == "/repos/owner/repo/branches/main/protection":
            return httpx.Response(200, json={
                "required_status_checks": {"strict": True, "contexts": ["CI"]},
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            })
        return httpx.Response(404, json={"message": "Not Found"})

    return (project, fwd, hot, branches, prs, seen,
            GitHubForwardPortWorkflow(transport=httpx.MockTransport(handler)))


def test_prepare_idempotent_on_pinned_main_never_changes_release():
    project, fwd, hot, branches, _, seen, service = simulator()
    first = service.prepare_forward_port(project, fwd, hot)
    second = service.prepare_forward_port(project, fwd, hot)
    assert first == second
    assert first.branch == "forward/FWD-1"
    assert branches["release/1.4"] == C
    assert sum(1 for verb, path in seen if verb == "POST" and
               path == "/repos/owner/repo/git/refs") == 1


def test_prepared_forward_port_uses_separate_work_branch_and_provenance():
    project, fwd, hot, branches, _, seen, service = simulator()
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    proof = service.prepare_forward_port(project, fwd, hot)
    assert proof.main_sha == A and proof.source_commits == (C,)
    assert proof.branch == "forward/FWD-1"
    assert proof.action == "APPLY_INTEGRATED_DELTA"
    assert branches["forward/FWD-1"] == A
    assert service.prepare_forward_port(project, fwd, hot).action == "APPLY_INTEGRATED_DELTA"
    assert sum(1 for verb, path in seen if verb == "POST" and
               path == "/repos/owner/repo/git/refs") == 1
    assert branches["release/1.4"] == C


def test_forward_pr_idempotent_and_separate_from_release():
    project, fwd, hot, branches, prs, _, service = simulator()
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    service.prepare_forward_port(project, fwd, hot)
    branches["forward/FWD-1"] = B
    first = service.ensure_forward_port_pr(
        project, fwd, hot, title="FWD-1 — port fix", expected_head_sha=B,
    )
    second = service.ensure_forward_port_pr(
        project, fwd, hot, title="FWD-1 — port fix", expected_head_sha=B,
    )
    assert first.created and not second.created and first.number == second.number == 150
    assert len(prs) == 1 and prs[0]["base"]["ref"] == "main"
    assert "Source-Hotfix-PR: #123" in prs[0]["body"]
    assert "Integrated-Commits: " + C in prs[0]["body"]


@pytest.mark.parametrize("kind", ["squash", "merge", "rebase"])
def test_all_integrated_methods_require_source_provenance(kind):
    project, fwd, hot, _, _, _, service = simulator(kind=kind)
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    proof = service.prepare_forward_port(project, fwd, hot)
    assert proof.method == fwd.forward_port_method
    assert proof.source_commits == fwd.integrated_commits


def test_already_present_requires_explicit_equivalence_not_empty_pr():
    project, fwd, hot, _, prs, _, service = simulator(already_present=True)
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    with pytest.raises(ForwardPortError, match="ALREADY_PRESENT"):
        service.prepare_forward_port(project, fwd, hot)
    assert not prs


def test_missing_transfer_provenance_and_retarget_block():
    project, fwd, hot, branches, prs, _, service = simulator(missing_provenance=True)
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    service.prepare_forward_port(project, fwd, hot)
    branches["forward/FWD-1"] = B
    with pytest.raises(ForwardPortError, match="PROVENANCE"):
        service.ensure_forward_port_pr(
            project, fwd, hot, title="FWD-1 — fix", expected_head_sha=B,
        )
    assert not prs


def test_moved_main_fails_closed_and_never_writes():
    project, fwd, hot, branches, _, seen, service = simulator(moved_main=True)
    fwd = replace(fwd, expected_work_branch="forward/FWD-1")
    project = replace(project, delivery_contexts=(hot, fwd))
    with pytest.raises(ReleaseWorkflowError, match="SOURCE_REF_MOVED"):
        service.prepare_forward_port(project, fwd, hot)
    assert not any(verb == "POST" for verb, _ in seen)
