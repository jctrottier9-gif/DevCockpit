from __future__ import annotations

import httpx

from app.domain.project import Project
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType
from app.infrastructure.github_review import GitHubReviewReader


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
MAIN = WorkItem(
    key="MAIN-A",
    type=WorkItemType.WORK,
    status=WorkItemStatus.READY,
    parent="#41",
    lane="MAIN",
    title="Main delivery",
)
PAR = WorkItem(
    key="PAR-B",
    type=WorkItemType.WORK,
    status=WorkItemStatus.READY,
    parent="#41",
    lane="PARALLEL",
    title="Parallel delivery",
)


def pull(number, key, sha, *, mergeable=None, auto_merge=None):
    return {
        "number": number,
        "title": f"{key} delivery",
        "body": f"Work-Item: {key}",
        "state": "open",
        "merged_at": None,
        "updated_at": "2026-10-05T01:00:00Z",
        "html_url": f"https://github.example/pr/{number}",
        "mergeable": mergeable,
        "auto_merge": auto_merge,
        "head": {"ref": f"{key.lower()}-branch", "sha": sha},
    }


def test_review_reader_uses_current_head_and_exact_workflow_attempt_jobs():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls":
            return httpx.Response(
                200,
                json=[
                    pull(10, "MAIN-A", "new-main"),
                    pull(11, "PAR-B", "new-par"),
                    pull(99, "OTHER", "other"),
                ],
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/10":
            return httpx.Response(
                200,
                json=pull(
                    10,
                    "MAIN-A",
                    "new-main",
                    mergeable=False,
                    auto_merge={"merge_method": "SQUASH"},
                ),
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/pulls/11":
            return httpx.Response(
                200,
                json=pull(11, "PAR-B", "new-par", mergeable=None),
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs":
            head = request.url.params["head_sha"]
            if head == "new-main":
                return httpx.Response(
                    200,
                    json={
                        "total_count": 2,
                        "workflow_runs": [
                            {
                                "id": 100,
                                "name": "CI",
                                "status": "completed",
                                "conclusion": "failure",
                                "run_attempt": 2,
                                "head_sha": "new-main",
                                "html_url": "https://github.example/actions/100",
                            },
                            {
                                "id": 90,
                                "name": "old CI",
                                "status": "completed",
                                "conclusion": "success",
                                "run_attempt": 1,
                                "head_sha": "old-main",
                                "html_url": "https://github.example/actions/90",
                            },
                        ],
                    },
                )
            assert head == "new-par"
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 200,
                            "name": "CI",
                            "status": "in_progress",
                            "conclusion": None,
                            "run_attempt": 1,
                            "head_sha": "new-par",
                            "html_url": "https://github.example/actions/200",
                        }
                    ],
                },
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs/100/attempts/2/jobs":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "jobs": [
                        {
                            "id": 1,
                            "name": "backend",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.example/jobs/1",
                            "started_at": "2026-10-05T01:01:00Z",
                            "completed_at": "2026-10-05T01:02:00Z",
                        },
                        {
                            "id": 2,
                            "name": "frontend",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://github.example/jobs/2",
                            "started_at": "2026-10-05T01:01:00Z",
                            "completed_at": "2026-10-05T01:03:00Z",
                        },
                    ],
                },
            )
        if path == "/repos/jctrottier9-gif/DevCockpit/actions/runs/200/attempts/1/jobs":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 3,
                            "name": "extension",
                            "status": "in_progress",
                            "conclusion": None,
                            "html_url": "https://github.example/jobs/3",
                            "started_at": "2026-10-05T01:04:00Z",
                            "completed_at": None,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    reader = GitHubReviewReader(transport=httpx.MockTransport(handler))
    evidence = reader.read(PROJECT, (MAIN, PAR))

    assert evidence.complete is True
    assert [(item.work_item_id, item.head_sha, item.ci_state) for item in evidence.pull_requests] == [
        ("MAIN-A", "new-main", "RED"),
        ("PAR-B", "new-par", "RUNNING"),
    ]
    main = evidence.pull_requests[0]
    assert main.auto_merge_enabled is True
    assert main.mergeable is False
    assert [(run.run_id, run.attempt, run.head_sha) for run in main.workflows] == [
        (100, 2, "new-main")
    ]
    assert [(job.name, job.conclusion) for job in main.workflows[0].jobs] == [
        ("backend", "failure"),
        ("frontend", "success"),
    ]
    assert not any("/actions/runs/90/" in path for path, _ in seen)
    assert any(
        path.endswith("/actions/runs/100/attempts/2/jobs")
        for path, _ in seen
    )
