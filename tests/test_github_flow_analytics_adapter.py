import httpx

from app.domain.flow_analytics import derive_flow_analytics
from app.domain.project import Project
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType
from app.infrastructure.github_flow_analytics import GitHubFlowAnalyticsReader


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
DC061 = WorkItem("DC-061", WorkItemType.WORK, WorkItemStatus.READY, "#1", "MAIN", "Flow Analytics")
DC060 = WorkItem("DC-060", WorkItemType.WORK, WorkItemStatus.DONE, "#1", "MAIN", "Attention Center")


def pull(number, title, branch, sha, *, body="", created="2026-10-02T10:00:00Z", merged=None):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "closed" if merged else "open",
        "created_at": created,
        "updated_at": merged or created,
        "merged_at": merged,
        "html_url": f"https://github.example/pr/{number}",
        "head": {"ref": branch, "sha": sha},
    }


def workflow(run_id, sha, conclusion, *, attempt=1, updated="2026-10-02T10:30:00Z"):
    return {
        "id": run_id,
        "name": "CI",
        "status": "completed",
        "conclusion": conclusion,
        "run_attempt": attempt,
        "head_sha": sha,
        "created_at": "2026-10-02T10:20:00Z",
        "updated_at": updated,
        "html_url": f"https://github.example/actions/{run_id}",
    }


def test_reader_uses_strict_pr_identity_commit_history_files_and_ci_attempts():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[
                pull(
                    34,
                    "DC-060 — Attention Center",
                    "dc-060-attention",
                    "unrelated",
                    body="DC-061 is intentionally out of scope.",
                    merged="2026-10-02T09:00:00Z",
                ),
                pull(
                    35,
                    "DC-061 — Flow Analytics",
                    "dc-061-flow-analytics",
                    "green-sha",
                    body="Work-Item: DC-061",
                    merged="2026-10-02T11:00:00Z",
                ),
            ])
        if path.endswith("/pulls/35/commits"):
            return httpx.Response(200, json=[
                {"sha": "red-sha", "commit": {"committer": {"date": "2026-10-02T09:00:00Z"}}},
                {"sha": "green-sha", "commit": {"committer": {"date": "2026-10-02T10:15:00Z"}}},
            ])
        if path.endswith("/pulls/35/files"):
            return httpx.Response(200, json=[{"filename": "app/domain/flow_analytics.py"}])
        if path.endswith("/actions/runs"):
            assert request.url.params["branch"] == "dc-061-flow-analytics"
            return httpx.Response(200, json={"workflow_runs": [
                workflow(100, "red-sha", "success", attempt=2, updated="2026-10-02T10:30:00Z"),
                workflow(101, "green-sha", "success", updated="2026-10-02T10:40:00Z"),
                workflow(999, "other-sha", "failure"),
            ]})
        if path.endswith("/actions/runs/100/attempts/1"):
            return httpx.Response(200, json=workflow(100, "red-sha", "failure", attempt=1, updated="2026-10-02T10:10:00Z"))
        raise AssertionError(f"unexpected request: {request.url}")

    reader = GitHubFlowAnalyticsReader(transport=httpx.MockTransport(handler))
    evidence = reader.read(PROJECT, (DC061,))
    projection = derive_flow_analytics(evidence)

    assert len(evidence.deliveries) == 1
    assert evidence.deliveries[0].pull_request.number == 35
    item = projection.deliveries[0]
    assert item.first_commit_at == "2026-10-02T09:00:00Z"
    assert item.ci_attempt_count == 3
    assert item.ci_red_attempt_count == 1
    assert item.recovered_after_red is True
    assert item.first_green_ci_at == "2026-10-02T10:30:00Z"
    assert not any("/pulls/34/commits" in path for path, _ in seen)


def test_docs_only_pr_is_excluded_using_real_changed_filenames():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[
                pull(34, "DC-060 — Attention Center", "dc-060-attention", "sha", merged="2026-10-02T11:00:00Z")
            ])
        if path.endswith("/pulls/34/commits"):
            return httpx.Response(200, json=[
                {"sha": "sha", "commit": {"committer": {"date": "2026-10-02T09:00:00Z"}}}
            ])
        if path.endswith("/pulls/34/files"):
            return httpx.Response(200, json=[{"filename": "docs/attention.md"}, {"filename": "README.md"}])
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        raise AssertionError(request.url)

    evidence = GitHubFlowAnalyticsReader(transport=httpx.MockTransport(handler)).read(PROJECT, (DC060,))
    projection = derive_flow_analytics(evidence)

    assert projection.deliveries == ()
    assert projection.aggregates.docs_only_excluded_count == 1
    assert projection.exclusions[0].work_item_id == "DC-060"


def test_multiple_open_strong_prs_fail_closed_for_work_item_without_affecting_other_items():
    other = WorkItem("DC-059", WorkItemType.WORK, WorkItemStatus.DONE, "#1", "MAIN", "Other")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[
                pull(40, "DC-061 — one", "dc-061-one", "a"),
                pull(41, "DC-061 — two", "dc-061-two", "b"),
                pull(39, "DC-059 — other", "dc-059-other", "c", merged="2026-10-02T08:00:00Z"),
            ])
        if path.endswith("/pulls/39/commits"):
            return httpx.Response(200, json=[{"sha": "c", "commit": {"committer": {"date": "2026-10-02T07:00:00Z"}}}])
        if path.endswith("/pulls/39/files"):
            return httpx.Response(200, json=[{"filename": "app/main.py"}])
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": []})
        raise AssertionError(request.url)

    evidence = GitHubFlowAnalyticsReader(transport=httpx.MockTransport(handler)).read(PROJECT, (DC061, other))

    assert [item.work_item.key for item in evidence.deliveries] == ["DC-059"]
    assert evidence.diagnostics[0].code == "AMBIGUOUS_DELIVERY"
    assert evidence.diagnostics[0].work_item_id == "DC-061"
