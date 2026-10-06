from __future__ import annotations

from dataclasses import replace
from urllib.parse import quote

import httpx

from app.application.executions import (
    ExecutionAuthorizationError,
    ExecutionPayloadError,
    ExecutionRepositoryNotFoundError,
    ExecutionSourceError,
)
from app.domain.execution import (
    BranchEvidence,
    ExecutionEvidence,
    PullRequestEvidence,
    WorkflowRunEvidence,
    branch_matches_work_item,
    pull_request_matches_work_item,
)
from app.domain.project import Project
from app.domain.roadmap import WorkItem


_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)


class GitHubExecutionReader:
    """Read-only GitHub adapter for branch, pull-request and Actions evidence."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def read(self, project: Project, work_item: WorkItem) -> ExecutionEvidence:
        owner, repository = project.repository_full_name.split("/", maxsplit=1)
        base_url = (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repository, safe='')}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DevCockpit",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers=headers,
            ) as client:
                repository_payload = self._request_json(client, base_url)
                default_branch = self._require_string(
                    repository_payload,
                    "default_branch",
                    context="repository",
                )

                pulls_payload = self._request_json(
                    client,
                    f"{base_url}/pulls",
                    params={
                        "state": "all",
                        "sort": "updated",
                        "direction": "desc",
                        "per_page": 100,
                    },
                )
                if not isinstance(pulls_payload, list):
                    raise ExecutionPayloadError("GitHub pull-request response must be an array")
                pull_requests = tuple(self._parse_pull_request(item) for item in pulls_payload)
                matching = tuple(
                    item
                    for item in pull_requests
                    if pull_request_matches_work_item(item, work_item.key)
                )
                open_matching = tuple(
                    item for item in matching if item.state == "open" and not item.merged
                )
                merged_matching = tuple(item for item in matching if item.merged)

                selected: PullRequestEvidence | None = None
                if len(open_matching) == 1:
                    selected = self._read_pull_request_detail(
                        client,
                        base_url,
                        open_matching[0],
                    )
                    pull_requests = tuple(
                        selected if item.number == selected.number else item
                        for item in pull_requests
                    )
                elif not open_matching and merged_matching:
                    selected = max(
                        merged_matching,
                        key=lambda item: (
                            item.merged_at or "",
                            item.updated_at or "",
                            item.number,
                        ),
                    )

                workflow_runs: tuple[WorkflowRunEvidence, ...] = ()
                branches: tuple[BranchEvidence, ...] = ()

                if selected is not None:
                    workflow_runs = self._read_workflow_runs(
                        client,
                        base_url,
                        selected.head_sha,
                    )
                elif len(open_matching) <= 1:
                    branches = self._read_candidate_branches(
                        client,
                        base_url,
                        default_branch,
                        work_item.key,
                    )

                return ExecutionEvidence(
                    default_branch=default_branch,
                    branches=branches,
                    pull_requests=pull_requests,
                    workflow_runs=workflow_runs,
                )
        except httpx.RequestError as exc:
            raise ExecutionSourceError("GitHub execution request failed") from exc

    def _request_json(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> object:
        response = client.get(url, params=params)
        if response.status_code in {401, 403}:
            raise ExecutionAuthorizationError("GitHub rejected execution evidence access")
        if response.status_code == 404:
            raise ExecutionRepositoryNotFoundError("GitHub execution resource was not found")
        if response.status_code >= 400:
            raise ExecutionSourceError(
                f"GitHub execution request failed with HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ExecutionPayloadError("GitHub execution response was not valid JSON") from exc

    def _read_pull_request_detail(
        self,
        client: httpx.Client,
        base_url: str,
        summary: PullRequestEvidence,
    ) -> PullRequestEvidence:
        payload = self._request_json(client, f"{base_url}/pulls/{summary.number}")
        detail = self._parse_pull_request(payload)
        mergeable = payload.get("mergeable") if isinstance(payload, dict) else None
        if mergeable is not None and not isinstance(mergeable, bool):
            raise ExecutionPayloadError("GitHub pull-request mergeable must be boolean or null")
        auto_merge = payload.get("auto_merge") if isinstance(payload, dict) else None
        if auto_merge is not None and not isinstance(auto_merge, dict):
            raise ExecutionPayloadError("GitHub pull-request auto_merge must be an object or null")
        mergeable_state = payload.get("mergeable_state") if isinstance(payload, dict) else None
        if mergeable_state is not None and not isinstance(mergeable_state, str):
            raise ExecutionPayloadError("GitHub pull-request mergeable_state must be text or null")

        behind_by: int | None = None
        if detail.base_sha is not None:
            compare = self._request_json(
                client,
                (
                    f"{base_url}/compare/"
                    f"{quote(detail.base_sha, safe='')}...{quote(detail.head_sha, safe='')}"
                ),
            )
            if not isinstance(compare, dict):
                raise ExecutionPayloadError("GitHub pull-request compare response must be an object")
            raw_behind = compare.get("behind_by")
            if not isinstance(raw_behind, int):
                raise ExecutionPayloadError("GitHub pull-request compare behind_by must be integer")
            behind_by = raw_behind

        return replace(
            detail,
            mergeable=mergeable,
            mergeable_state=mergeable_state,
            behind_by=behind_by,
            auto_merge_enabled=auto_merge is not None,
        )

    def _read_candidate_branches(
        self,
        client: httpx.Client,
        base_url: str,
        default_branch: str,
        work_item_key: str,
    ) -> tuple[BranchEvidence, ...]:
        payload = self._request_json(
            client,
            f"{base_url}/branches",
            params={"per_page": 100},
        )
        if not isinstance(payload, list):
            raise ExecutionPayloadError("GitHub branches response must be an array")

        branches: list[BranchEvidence] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ExecutionPayloadError("GitHub branch entry must be an object")
            name = self._require_string(item, "name", context="branch")
            if not branch_matches_work_item(name, work_item_key):
                continue
            commit = item.get("commit")
            if not isinstance(commit, dict):
                raise ExecutionPayloadError("GitHub branch commit must be an object")
            sha = self._require_string(commit, "sha", context="branch commit")
            compare = self._request_json(
                client,
                (
                    f"{base_url}/compare/"
                    f"{quote(default_branch, safe='')}...{quote(name, safe='')}"
                ),
            )
            if not isinstance(compare, dict):
                raise ExecutionPayloadError("GitHub compare response must be an object")
            ahead_by = compare.get("ahead_by")
            if not isinstance(ahead_by, int):
                raise ExecutionPayloadError("GitHub compare ahead_by must be an integer")
            last_activity_at = self._latest_compare_commit_date(compare)
            branches.append(
                BranchEvidence(
                    name=name,
                    sha=sha,
                    ahead_by=ahead_by,
                    last_activity_at=last_activity_at,
                )
            )
        return tuple(branches)

    @staticmethod
    def _latest_compare_commit_date(compare: dict[str, object]) -> str | None:
        commits = compare.get("commits")
        if not isinstance(commits, list):
            return None

        dates: list[str] = []
        for item in commits:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit")
            if not isinstance(commit, dict):
                continue
            for identity_key in ("committer", "author"):
                identity = commit.get(identity_key)
                if not isinstance(identity, dict):
                    continue
                value = identity.get("date")
                if isinstance(value, str) and value:
                    dates.append(value)
                    break
        return max(dates) if dates else None

    def _read_workflow_runs(
        self,
        client: httpx.Client,
        base_url: str,
        head_sha: str,
    ) -> tuple[WorkflowRunEvidence, ...]:
        payload = self._request_json(
            client,
            f"{base_url}/actions/runs",
            params={
                "head_sha": head_sha,
                "event": "pull_request",
                "per_page": 100,
            },
        )
        if not isinstance(payload, dict):
            raise ExecutionPayloadError("GitHub workflow-runs response must be an object")
        raw_runs = payload.get("workflow_runs")
        if not isinstance(raw_runs, list):
            raise ExecutionPayloadError("GitHub workflow_runs must be an array")

        runs: list[WorkflowRunEvidence] = []
        for item in raw_runs:
            if not isinstance(item, dict):
                raise ExecutionPayloadError("GitHub workflow run must be an object")
            run_head_sha = self._require_string(item, "head_sha", context="workflow run")
            if run_head_sha != head_sha:
                continue
            run_id = item.get("id")
            attempt = item.get("run_attempt", 1)
            if not isinstance(run_id, int) or not isinstance(attempt, int):
                raise ExecutionPayloadError("GitHub workflow run identity must be integer")
            status = self._require_string(item, "status", context="workflow run")
            conclusion = item.get("conclusion")
            if conclusion is not None and not isinstance(conclusion, str):
                raise ExecutionPayloadError("GitHub workflow conclusion must be text or null")
            failed_jobs: tuple[str, ...] = ()
            if status == "completed" and conclusion in _FAILURE_CONCLUSIONS:
                failed_jobs = self._read_failed_jobs(client, base_url, run_id)

            name = item.get("name")
            if not isinstance(name, str) or not name:
                name = f"workflow-{run_id}"
            url = item.get("html_url")
            if url is not None and not isinstance(url, str):
                url = None
            created_at = item.get("created_at")
            updated_at = item.get("updated_at")
            if created_at is not None and not isinstance(created_at, str):
                raise ExecutionPayloadError("GitHub workflow created_at must be text or null")
            if updated_at is not None and not isinstance(updated_at, str):
                raise ExecutionPayloadError("GitHub workflow updated_at must be text or null")
            runs.append(
                WorkflowRunEvidence(
                    run_id=run_id,
                    name=name,
                    status=status,
                    conclusion=conclusion,
                    attempt=attempt,
                    head_sha=run_head_sha,
                    url=url,
                    failed_jobs=failed_jobs,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return tuple(runs)

    def _read_failed_jobs(
        self,
        client: httpx.Client,
        base_url: str,
        run_id: int,
    ) -> tuple[str, ...]:
        payload = self._request_json(
            client,
            f"{base_url}/actions/runs/{run_id}/jobs",
            params={"filter": "latest", "per_page": 100},
        )
        if not isinstance(payload, dict):
            raise ExecutionPayloadError("GitHub workflow-jobs response must be an object")
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise ExecutionPayloadError("GitHub workflow jobs must be an array")
        names: list[str] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                raise ExecutionPayloadError("GitHub workflow job must be an object")
            conclusion = item.get("conclusion")
            if conclusion not in _FAILURE_CONCLUSIONS:
                continue
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(sorted(set(names)))

    @staticmethod
    def _parse_pull_request(payload: object) -> PullRequestEvidence:
        if not isinstance(payload, dict):
            raise ExecutionPayloadError("GitHub pull-request entry must be an object")
        head = payload.get("head")
        if not isinstance(head, dict):
            raise ExecutionPayloadError("GitHub pull-request head must be an object")
        base = payload.get("base")
        if base is not None and not isinstance(base, dict):
            raise ExecutionPayloadError("GitHub pull-request base must be an object or null")
        number = payload.get("number")
        if not isinstance(number, int):
            raise ExecutionPayloadError("GitHub pull-request number must be an integer")
        title = GitHubExecutionReader._require_string(
            payload,
            "title",
            context="pull request",
        )
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ExecutionPayloadError("GitHub pull-request body must be text or null")
        branch = GitHubExecutionReader._require_string(head, "ref", context="pull-request head")
        head_sha = GitHubExecutionReader._require_string(head, "sha", context="pull-request head")
        state = GitHubExecutionReader._require_string(payload, "state", context="pull request")
        merged_at = payload.get("merged_at")
        if merged_at is not None and not isinstance(merged_at, str):
            raise ExecutionPayloadError("GitHub pull-request merged_at must be text or null")
        created_at = payload.get("created_at")
        if created_at is not None and not isinstance(created_at, str):
            raise ExecutionPayloadError("GitHub pull-request created_at must be text or null")
        updated_at = payload.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise ExecutionPayloadError("GitHub pull-request updated_at must be text or null")
        url = payload.get("html_url")
        if url is not None and not isinstance(url, str):
            url = None
        mergeable = payload.get("mergeable")
        if mergeable is not None and not isinstance(mergeable, bool):
            mergeable = None
        auto_merge = payload.get("auto_merge")
        if auto_merge is not None and not isinstance(auto_merge, dict):
            raise ExecutionPayloadError("GitHub pull-request auto_merge must be an object or null")
        mergeable_state = payload.get("mergeable_state")
        if mergeable_state is not None and not isinstance(mergeable_state, str):
            mergeable_state = None
        base_branch = None
        base_sha = None
        if isinstance(base, dict):
            raw_base_branch = base.get("ref")
            raw_base_sha = base.get("sha")
            if isinstance(raw_base_branch, str) and raw_base_branch:
                base_branch = raw_base_branch
            if isinstance(raw_base_sha, str) and raw_base_sha:
                base_sha = raw_base_sha
        return PullRequestEvidence(
            number=number,
            title=title,
            body=body,
            branch=branch,
            head_sha=head_sha,
            state=state,
            merged=merged_at is not None,
            mergeable=mergeable,
            base_branch=base_branch,
            base_sha=base_sha,
            mergeable_state=mergeable_state,
            auto_merge_enabled=auto_merge is not None,
            url=url,
            updated_at=updated_at,
            merged_at=merged_at,
            created_at=created_at,
        )

    @staticmethod
    def _require_string(
        payload: object,
        key: str,
        *,
        context: str,
    ) -> str:
        if not isinstance(payload, dict):
            raise ExecutionPayloadError(f"GitHub {context} payload must be an object")
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ExecutionPayloadError(f"GitHub {context} {key} must be non-empty text")
        return value
