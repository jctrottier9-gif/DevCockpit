import httpx
import pytest

from app.application.executions import ExecutionAuthorizationError
from app.domain.execution import derive_execution_projection, ExecutionState
from app.domain.project import Project
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType
from app.infrastructure.github_execution import GitHubExecutionReader


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
WORK_ITEM = WorkItem(
    key="DC-021",
    type=WorkItemType.WORK,
    status=WorkItemStatus.READY,
    parent="#1",
    lane="MAIN",
    title="projection exécution et suivi CI",
)


def reader_for(handler) -> GitHubExecutionReader:
    return GitHubExecutionReader(transport=httpx.MockTransport(handler))


def pull_payload(
    *,
    number: int = 22,
    title: str = "DC-021 — execution projection",
    body: str = "",
    branch: str = "dc-021-execution-ci",
    sha: str = "abc123",
    state: str = "open",
    merged_at=None,
    mergeable=None,
    auto_merge=None,
    base_sha=None,
    mergeable_state=None,
):
    payload = {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "merged_at": merged_at,
        "updated_at": "2026-10-01T12:00:00Z",
        "html_url": f"https://github.example/pr/{number}",
        "mergeable": mergeable,
        "auto_merge": auto_merge,
        "mergeable_state": mergeable_state,
        "head": {"ref": branch, "sha": sha},
    }
    if base_sha is not None:
        payload["base"] = {"ref": "main", "sha": base_sha}
    return payload


def test_reads_current_pr_head_ci_and_failed_jobs() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(200, json=[pull_payload()])
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/22":
            return httpx.Response(200, json=pull_payload(mergeable=True))
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs":
            assert request.url.params["head_sha"] == "abc123"
            assert request.url.params["event"] == "pull_request"
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 123,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                            "run_attempt": 1,
                            "head_sha": "abc123",
                            "html_url": "https://github.example/actions/123",
                        }
                    ]
                },
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs/123/jobs":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"name": "backend / pytest", "conclusion": "failure"},
                        {"name": "frontend / build", "conclusion": "success"},
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)
    projection = derive_execution_projection(WORK_ITEM, evidence)

    assert projection.state is ExecutionState.CI_RED
    assert projection.ci is not None
    assert projection.ci.failed_jobs == ("backend / pytest",)
    assert "/repos/jctrottier9-gif/DevCockpit/branches" not in seen


def test_reads_auto_merge_state_from_pull_request_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(200, json=[pull_payload()])
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/22":
            return httpx.Response(
                200,
                json=pull_payload(
                    mergeable=True,
                    auto_merge={
                        "enabled_by": {"login": "jctrottier9-gif"},
                        "merge_method": "SQUASH",
                    },
                ),
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs":
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 456,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "head_sha": "abc123",
                            "html_url": "https://github.example/actions/456",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)

    assert len(evidence.pull_requests) == 1
    assert evidence.pull_requests[0].mergeable is True
    assert evidence.pull_requests[0].auto_merge_enabled is True
    projection = derive_execution_projection(WORK_ITEM, evidence)
    assert projection.state is ExecutionState.READY_TO_MERGE
    assert projection.next_action.value == "WAIT_AUTO_MERGE"


def test_reads_base_sha_and_behind_count_for_open_pr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(
                200,
                json=[pull_payload(base_sha="base123")],
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/22":
            return httpx.Response(
                200,
                json=pull_payload(
                    base_sha="base123",
                    mergeable=True,
                    mergeable_state="behind",
                ),
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/branches/main":
            return httpx.Response(
                200,
                json={"name": "main", "commit": {"sha": "current-main"}},
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/compare/current-main...abc123":
            return httpx.Response(
                200,
                json={"behind_by": 1, "ahead_by": 2},
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs":
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 456,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "head_sha": "abc123",
                            "html_url": "https://github.example/actions/456",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)
    pull_request = evidence.pull_requests[0]
    projection = derive_execution_projection(WORK_ITEM, evidence)

    assert pull_request.base_branch == "main"
    assert pull_request.base_sha == "current-main"
    assert pull_request.behind_by == 1
    assert pull_request.mergeable_state == "behind"
    assert projection.state is ExecutionState.BASE_OUTDATED
    assert projection.next_action.value == "SYNC_BRANCH"


def test_mergeable_state_behind_blocks_ready_to_merge_when_live_compare_reports_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(
                200,
                json=[pull_payload(base_sha="historical-base")],
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/22":
            return httpx.Response(
                200,
                json=pull_payload(
                    base_sha="historical-base",
                    mergeable=True,
                    mergeable_state="behind",
                    auto_merge={"enabled_by": {"login": "jctrottier9-gif"}},
                ),
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/branches/main":
            return httpx.Response(
                200,
                json={"name": "main", "commit": {"sha": "current-main"}},
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/compare/current-main...abc123":
            return httpx.Response(200, json={"behind_by": 0, "ahead_by": 1})
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs":
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 456,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "head_sha": "abc123",
                            "html_url": "https://github.example/actions/456",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)
    projection = derive_execution_projection(WORK_ITEM, evidence)

    assert evidence.pull_requests[0].base_sha == "current-main"
    assert evidence.pull_requests[0].behind_by == 0
    assert evidence.pull_requests[0].mergeable_state == "behind"
    assert projection.state is ExecutionState.BASE_OUTDATED
    assert projection.next_action.value == "SYNC_BRANCH"


def test_branch_is_developing_only_when_compare_reports_ahead() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(200, json=[])
        if path == "/repos/jctrottier9-gif/DevCockpit/branches":
            return httpx.Response(
                200,
                json=[
                    {"name": "old-unrelated", "commit": {"sha": "old"}},
                    {"name": "dc-021-execution-ci", "commit": {"sha": "abc123"}},
                ],
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/compare/main...dc-021-execution-ci":
            return httpx.Response(
                200,
                json={
                    "ahead_by": 2,
                    "commits": [
                        {
                            "sha": "abc123",
                            "commit": {
                                "committer": {
                                    "date": "2026-10-03T08:15:00Z"
                                }
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)
    projection = derive_execution_projection(WORK_ITEM, evidence)

    assert projection.state is ExecutionState.DEVELOPING
    assert projection.branch is not None
    assert projection.branch.ahead_by == 2
    assert projection.branch.last_activity_at == "2026-10-03T08:15:00Z"


def test_incidental_body_mention_does_not_trigger_pr_ci_resolution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(
                200,
                json=[
                    pull_payload(
                        number=21,
                        title="DC-020 — GitHub projects and canonical roadmap parser",
                        branch="dc-020-roadmap-projects",
                        body="DC-021 is intentionally out of scope.",
                    )
                ],
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/branches":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)

    assert derive_execution_projection(WORK_ITEM, evidence).state is ExecutionState.READY


@pytest.mark.parametrize("status", [401, 403])
def test_github_authorization_failure_is_typed(status: int) -> None:
    reader = reader_for(lambda _: httpx.Response(status))

    with pytest.raises(ExecutionAuthorizationError):
        reader.read(PROJECT, WORK_ITEM)
