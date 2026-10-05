from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.cockpit_panels import (
    PanelDiagnostic,
    ReviewEvidence,
    ReviewJobEvidence,
    ReviewPullRequestEvidence,
    ReviewWorkflowEvidence,
    review_ci_state,
)
from app.application.executions import (
    ExecutionAuthorizationError,
    ExecutionPayloadError,
    ExecutionRepositoryNotFoundError,
    ExecutionSourceError,
)
from app.domain.execution import PullRequestEvidence, pull_request_matches_work_item
from app.domain.project import Project
from app.domain.roadmap import WorkItem


class GitHubReviewReader:
    """On-demand GitHub PR/Actions reader for the Reviewer cockpit surface."""

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

    def read(
        self,
        project: Project,
        work_items: tuple[WorkItem, ...],
    ) -> ReviewEvidence:
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

        diagnostics: list[PanelDiagnostic] = []
        complete = True
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers=headers,
            ) as client:
                raw_pulls, pulls_complete = self._paged_items(
                    client,
                    f"{base_url}/pulls",
                    params={"state": "open", "sort": "updated", "direction": "desc"},
                    max_pages=20,
                )
                complete = complete and pulls_complete
                if not pulls_complete:
                    diagnostics.append(
                        PanelDiagnostic(
                            "REVIEW_PULL_REQUESTS_PARTIAL",
                            "Open pull-request pagination reached the bounded reader limit.",
                        )
                    )

                matches: list[tuple[WorkItem, PullRequestEvidence]] = []
                for raw in raw_pulls:
                    summary = self._parse_pull_request(raw)
                    matching_items = tuple(
                        item
                        for item in work_items
                        if pull_request_matches_work_item(summary, item.key)
                    )
                    if len(matching_items) > 1:
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_PR_WORK_ITEM_AMBIGUOUS",
                                f"PR #{summary.number} strongly matches multiple active WorkItems.",
                            )
                        )
                        continue
                    if len(matching_items) == 1:
                        matches.append((matching_items[0], summary))

                pull_requests: list[ReviewPullRequestEvidence] = []
                counts: dict[str, int] = {}
                for work_item, summary in matches:
                    detail_payload = self._request_json(
                        client,
                        f"{base_url}/pulls/{summary.number}",
                    )
                    detail = self._parse_pull_request(detail_payload)
                    if not pull_request_matches_work_item(detail, work_item.key):
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_PR_IDENTITY_CHANGED",
                                f"PR #{detail.number} no longer strongly matches {work_item.key}.",
                                work_item.key,
                            )
                        )
                        continue
                    counts[work_item.key] = counts.get(work_item.key, 0) + 1

                    workflows, workflows_complete = self._read_workflows(
                        client,
                        base_url,
                        detail.head_sha,
                    )
                    complete = complete and workflows_complete
                    if not workflows_complete:
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_WORKFLOWS_PARTIAL",
                                f"Workflow pagination for PR #{detail.number} is partial.",
                                work_item.key,
                            )
                        )
                    pull_requests.append(
                        ReviewPullRequestEvidence(
                            work_item_id=work_item.key,
                            work_item_title=work_item.title,
                            lane=work_item.lane,
                            number=detail.number,
                            title=detail.title,
                            branch=detail.branch,
                            head_sha=detail.head_sha,
                            url=detail.url,
                            mergeable=detail.mergeable,
                            auto_merge_enabled=detail.auto_merge_enabled,
                            ci_state=review_ci_state(workflows),
                            workflows=workflows,
                        )
                    )

                for work_item_id, count in counts.items():
                    if count > 1:
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_MULTIPLE_OPEN_PRS",
                                f"{work_item_id} has {count} strongly associated open pull requests.",
                                work_item_id,
                            )
                        )

                order = {item.key: index for index, item in enumerate(work_items)}
                pull_requests.sort(key=lambda item: (order.get(item.work_item_id, 999999), item.number))
                return ReviewEvidence(
                    pull_requests=tuple(pull_requests),
                    complete=complete,
                    diagnostics=tuple(diagnostics),
                )
        except httpx.RequestError as exc:
            raise ExecutionSourceError("GitHub Reviewer request failed") from exc

    def _read_workflows(
        self,
        client: httpx.Client,
        base_url: str,
        head_sha: str,
    ) -> tuple[tuple[ReviewWorkflowEvidence, ...], bool]:
        raw_runs, runs_complete = self._paged_items(
            client,
            f"{base_url}/actions/runs",
            params={"head_sha": head_sha, "event": "pull_request"},
            payload_key="workflow_runs",
            max_pages=10,
        )
        workflows: list[ReviewWorkflowEvidence] = []
        complete = runs_complete
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
            name = item.get("name")
            if not isinstance(name, str) or not name:
                name = f"workflow-{run_id}"
            url = item.get("html_url")
            if url is not None and not isinstance(url, str):
                url = None

            jobs, jobs_complete = self._read_attempt_jobs(
                client,
                base_url,
                run_id,
                attempt,
            )
            complete = complete and jobs_complete
            workflows.append(
                ReviewWorkflowEvidence(
                    run_id=run_id,
                    name=name,
                    status=status,
                    conclusion=conclusion,
                    attempt=attempt,
                    head_sha=run_head_sha,
                    url=url,
                    jobs=jobs,
                    jobs_complete=jobs_complete,
                )
            )
        workflows.sort(key=lambda item: (item.run_id, item.attempt), reverse=True)
        return tuple(workflows), complete

    def _read_attempt_jobs(
        self,
        client: httpx.Client,
        base_url: str,
        run_id: int,
        attempt: int,
    ) -> tuple[tuple[ReviewJobEvidence, ...], bool]:
        raw_jobs, complete = self._paged_items(
            client,
            f"{base_url}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            params={},
            payload_key="jobs",
            max_pages=20,
        )
        jobs: list[ReviewJobEvidence] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                raise ExecutionPayloadError("GitHub workflow job must be an object")
            job_id = item.get("id")
            if not isinstance(job_id, int):
                raise ExecutionPayloadError("GitHub workflow job id must be integer")
            name = self._require_string(item, "name", context="workflow job")
            status = self._require_string(item, "status", context="workflow job")
            conclusion = item.get("conclusion")
            if conclusion is not None and not isinstance(conclusion, str):
                raise ExecutionPayloadError("GitHub workflow job conclusion must be text or null")
            url = item.get("html_url")
            started_at = item.get("started_at")
            completed_at = item.get("completed_at")
            for value, field in ((url, "html_url"), (started_at, "started_at"), (completed_at, "completed_at")):
                if value is not None and not isinstance(value, str):
                    raise ExecutionPayloadError(f"GitHub workflow job {field} must be text or null")
            jobs.append(
                ReviewJobEvidence(
                    job_id=job_id,
                    name=name,
                    status=status,
                    conclusion=conclusion,
                    url=url,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        jobs.sort(key=lambda item: item.job_id)
        return tuple(jobs), complete

    def _paged_items(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, object],
        payload_key: str | None = None,
        max_pages: int,
    ) -> tuple[list[object], bool]:
        items: list[object] = []
        for page in range(1, max_pages + 1):
            page_params = {**params, "per_page": 100, "page": page}
            payload = self._request_json(client, url, params=page_params)
            if payload_key is None:
                if not isinstance(payload, list):
                    raise ExecutionPayloadError("GitHub paginated response must be an array")
                chunk = payload
                total_count = None
            else:
                if not isinstance(payload, dict):
                    raise ExecutionPayloadError("GitHub paginated response must be an object")
                chunk = payload.get(payload_key)
                total_count = payload.get("total_count")
                if not isinstance(chunk, list):
                    raise ExecutionPayloadError(
                        f"GitHub paginated response {payload_key} must be an array"
                    )
                if total_count is not None and not isinstance(total_count, int):
                    raise ExecutionPayloadError("GitHub paginated total_count must be integer")
            items.extend(chunk)
            if total_count is not None and len(items) >= total_count:
                return items, True
            if len(chunk) < 100:
                return items, True
        return items, False

    def _request_json(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> object:
        response = client.get(url, params=params)
        if response.status_code in {401, 403}:
            raise ExecutionAuthorizationError("GitHub rejected Reviewer evidence access")
        if response.status_code == 404:
            raise ExecutionRepositoryNotFoundError("GitHub Reviewer resource was not found")
        if response.status_code >= 400:
            raise ExecutionSourceError(
                f"GitHub Reviewer request failed with HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ExecutionPayloadError("GitHub Reviewer response was not valid JSON") from exc

    @staticmethod
    def _parse_pull_request(payload: object) -> PullRequestEvidence:
        if not isinstance(payload, dict):
            raise ExecutionPayloadError("GitHub pull-request entry must be an object")
        head = payload.get("head")
        if not isinstance(head, dict):
            raise ExecutionPayloadError("GitHub pull-request head must be an object")
        number = payload.get("number")
        if not isinstance(number, int):
            raise ExecutionPayloadError("GitHub pull-request number must be integer")
        title = GitHubReviewReader._require_string(payload, "title", context="pull request")
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ExecutionPayloadError("GitHub pull-request body must be text or null")
        branch = GitHubReviewReader._require_string(head, "ref", context="pull-request head")
        head_sha = GitHubReviewReader._require_string(head, "sha", context="pull-request head")
        state = GitHubReviewReader._require_string(payload, "state", context="pull request")
        merged_at = payload.get("merged_at")
        updated_at = payload.get("updated_at")
        url = payload.get("html_url")
        mergeable = payload.get("mergeable")
        auto_merge = payload.get("auto_merge")
        if mergeable is not None and not isinstance(mergeable, bool):
            mergeable = None
        if auto_merge is not None and not isinstance(auto_merge, dict):
            raise ExecutionPayloadError("GitHub pull-request auto_merge must be object or null")
        for value, field in ((merged_at, "merged_at"), (updated_at, "updated_at"), (url, "html_url")):
            if value is not None and not isinstance(value, str):
                raise ExecutionPayloadError(f"GitHub pull-request {field} must be text or null")
        return PullRequestEvidence(
            number=number,
            title=title,
            body=body,
            branch=branch,
            head_sha=head_sha,
            state=state,
            merged=merged_at is not None,
            mergeable=mergeable,
            auto_merge_enabled=auto_merge is not None,
            url=url,
            updated_at=updated_at,
            merged_at=merged_at,
        )

    @staticmethod
    def _require_string(payload: object, key: str, *, context: str) -> str:
        if not isinstance(payload, dict):
            raise ExecutionPayloadError(f"GitHub {context} payload must be object")
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ExecutionPayloadError(f"GitHub {context} {key} must be non-empty text")
        return value
