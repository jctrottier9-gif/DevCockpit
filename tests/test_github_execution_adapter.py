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
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "merged_at": merged_at,
        "updated_at": "2026-10-01T12:00:00Z",
        "html_url": f"https://github.example/pr/{number}",
        "mergeable": mergeable,
        "head": {"ref": branch, "sha": sha},
    }


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
            return httpx.Response(200, json={"ahead_by": 2})
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    evidence = reader_for(handler).read(PROJECT, WORK_ITEM)
    projection = derive_execution_projection(WORK_ITEM, evidence)

    assert projection.state is ExecutionState.DEVELOPING
    assert projection.branch is not None
    assert projection.branch.ahead_by == 2


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
