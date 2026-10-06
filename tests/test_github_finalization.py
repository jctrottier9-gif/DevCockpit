import json

import httpx

from app.infrastructure.github_finalization import GitHubPullRequestFinalizer
from app.domain.pr_finalization import FinalizationAttemptStatus
from app.domain.project import Project


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


def detail(*, head="head-1", base="base-1", mergeable=True, mergeable_state="clean", merged=False):
    return {
        "number": 71,
        "state": "closed" if merged else "open",
        "merged_at": "2026-10-05T21:00:00Z" if merged else None,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "head": {"ref": "dc-071", "sha": head},
        "base": {"ref": "main", "sha": base},
    }


def test_sync_branch_sends_expected_head_sha():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/pulls/71"):
            return httpx.Response(200, json=detail())
        if request.method == "PUT" and request.url.path.endswith("/pulls/71/update-branch"):
            assert json.loads(request.content) == {"expected_head_sha": "head-1"}
            return httpx.Response(202, json={"message": "Updating pull request branch."})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = GitHubPullRequestFinalizer(
        transport=httpx.MockTransport(handler)
    ).sync_branch(
        PROJECT,
        pr_number=71,
        expected_head_sha="head-1",
        expected_base_sha="base-1",
    )

    assert result.status is FinalizationAttemptStatus.SUCCEEDED
    assert seen == [
        ("GET", "/repos/jctrottier9-gif/DevCockpit/pulls/71"),
        ("PUT", "/repos/jctrottier9-gif/DevCockpit/pulls/71/update-branch"),
    ]


def test_sync_branch_fails_closed_when_base_moved_before_mutation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/pulls/71"):
            return httpx.Response(200, json=detail(base="base-2"))
        raise AssertionError("no mutation is permitted after base moved")

    result = GitHubPullRequestFinalizer(
        transport=httpx.MockTransport(handler)
    ).sync_branch(
        PROJECT,
        pr_number=71,
        expected_head_sha="head-1",
        expected_base_sha="base-1",
    )

    assert result.status is FinalizationAttemptStatus.STALE
    assert result.error_code == "BASE_MOVED"


def test_merge_revalidates_current_head_ci_and_uses_expected_sha():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(
                200,
                json={
                    "allow_merge_commit": True,
                    "allow_squash_merge": True,
                    "allow_rebase_merge": False,
                },
            )
        if request.method == "GET" and path.endswith("/pulls/71"):
            return httpx.Response(200, json=detail())
        if request.method == "GET" and path.endswith("/actions/runs"):
            assert request.url.params["head_sha"] == "head-1"
            assert request.url.params["event"] == "pull_request"
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": "head-1",
                        }
                    ]
                },
            )
        if request.method == "PUT" and path.endswith("/pulls/71/merge"):
            assert json.loads(request.content) == {
                "sha": "head-1",
                "merge_method": "merge",
            }
            return httpx.Response(
                200,
                json={"merged": True, "sha": "merge-sha", "message": "merged"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = GitHubPullRequestFinalizer(
        transport=httpx.MockTransport(handler)
    ).merge_pull_request(
        PROJECT,
        pr_number=71,
        expected_head_sha="head-1",
    )

    assert result.status is FinalizationAttemptStatus.SUCCEEDED
    assert result.resulting_head_sha == "merge-sha"
    assert ("PUT", "/repos/jctrottier9-gif/DevCockpit/pulls/71/merge") in seen


def test_merge_fails_closed_when_head_moved():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"allow_merge_commit": True})
        if request.method == "GET" and path.endswith("/pulls/71"):
            return httpx.Response(200, json=detail(head="head-2"))
        raise AssertionError("CI and merge must not be called after head moved")

    result = GitHubPullRequestFinalizer(
        transport=httpx.MockTransport(handler)
    ).merge_pull_request(
        PROJECT,
        pr_number=71,
        expected_head_sha="head-1",
    )

    assert result.status is FinalizationAttemptStatus.STALE
    assert result.error_code == "HEAD_MOVED"


def test_merge_refusal_is_returned_as_observable_blocker():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"allow_merge_commit": True})
        if request.method == "GET" and path.endswith("/pulls/71"):
            return httpx.Response(200, json=detail())
        if request.method == "GET" and path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": "head-1",
                        }
                    ]
                },
            )
        if request.method == "PUT" and path.endswith("/pulls/71/merge"):
            return httpx.Response(
                405,
                json={"message": "Required approving review is missing"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    result = GitHubPullRequestFinalizer(
        transport=httpx.MockTransport(handler)
    ).merge_pull_request(
        PROJECT,
        pr_number=71,
        expected_head_sha="head-1",
    )

    assert result.status is FinalizationAttemptStatus.BLOCKED
    assert result.error_code == "MERGE_BLOCKED"
    assert "approving review" in result.message
