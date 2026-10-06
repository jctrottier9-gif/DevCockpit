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
                    stable_snapshot = None
                    identity_changed = False
                    for _snapshot_attempt in range(2):
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
                            identity_changed = True
                            break

                        workflows, workflows_complete = self._read_workflows(
                            client,
                            base_url,
                            detail.head_sha,
                        )
                        confirm_payload = self._request_json(
                            client,
                            f"{base_url}/pulls/{summary.number}",
                        )
                        confirm = self._parse_pull_request(confirm_payload)
                        if (
                            confirm.head_sha != detail.head_sha
                            or confirm.base_sha != detail.base_sha
                            or not pull_request_matches_work_item(confirm, work_item.key)
                        ):
                            continue

                        current_signature, signature_complete = self._read_workflow_signature(
                            client,
                            base_url,
                            detail.head_sha,
                        )
                        if current_signature != self._workflow_signature(workflows):
                            continue

                        stable_snapshot = (
                            confirm,
                            workflows,
                            workflows_complete and signature_complete,
                        )
                        break

                    if identity_changed:
                        continue
                    if stable_snapshot is None:
                        complete = False
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_EVIDENCE_CHANGED_DURING_READ",
                                (
                                    f"PR #{summary.number} head or workflow attempt changed "
                                    "while Reviewer evidence was being read."
                                ),
                                work_item.key,
                            )
                        )
                        continue

                    detail, workflows, snapshot_complete = stable_snapshot
                    behind_by = self._read_behind_by(client, base_url, detail)
                    ci_state = review_ci_state(workflows)
                    finalization_state = self._finalization_state(
                        detail,
                        ci_state=ci_state,
                        behind_by=behind_by,
                    )
                    counts[work_item.key] = counts.get(work_item.key, 0) + 1
                    complete = complete and snapshot_complete
                    if not snapshot_complete:
                        diagnostics.append(
                            PanelDiagnostic(
                                "REVIEW_WORKFLOWS_PARTIAL",
                                f"Workflow or job pagination for PR #{detail.number} is partial.",
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
                            base_branch=detail.base_branch,
                            base_sha=detail.base_sha,
                            behind_by=behind_by,
                            finalization_state=finalization_state,
                            finalization_detail=None,
                            ci_state=ci_state,
                            workflows=workflows,
                            created_at=detail.created_at,
                            updated_at=detail.updated_at,
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

    def _read_behind_by(
        self,
        client: httpx.Client,
        base_url: str,
        pull_request: PullRequestEvidence,
    ) -> int | None:
        if pull_request.base_sha is None:
            return None
        payload = self._request_json(
            client,
            (
                f"{base_url}/compare/"
                f"{quote(pull_request.base_sha, safe='')}..."
                f"{quote(pull_request.head_sha, safe='')}"
            ),
        )
        if not isinstance(payload, dict):
            raise ExecutionPayloadError("GitHub Reviewer compare response must be object")
        behind_by = payload.get("behind_by")
        if not isinstance(behind_by, int):
            raise ExecutionPayloadError("GitHub Reviewer compare behind_by must be integer")
        return behind_by

    @staticmethod
    def _finalization_state(
        pull_request: PullRequestEvidence,
        *,
        ci_state: str,
        behind_by: int | None,
    ) -> str | None:
        if ci_state != "GREEN":
            return None
        if (behind_by or 0) > 0:
            return "BASE_OUTDATED"
        if pull_request.auto_merge_enabled:
            return "WAIT_AUTO_MERGE"
        if pull_request.mergeable is True:
            return "FINALIZE_BY_DEVCOCKPIT"
        return None

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
            created_at = item.get("created_at")
            updated_at = item.get("updated_at")
            if created_at is not None and not isinstance(created_at, str):
                raise ExecutionPayloadError("GitHub workflow created_at must be text or null")
            if updated_at is not None and not isinstance(updated_at, str):
                raise ExecutionPayloadError("GitHub workflow updated_at must be text or null")

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
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        workflows.sort(key=lambda item: (item.run_id, item.attempt), reverse=True)
        return tuple(workflows), complete

    def _read_workflow_signature(
        self,
        client: httpx.Client,
        base_url: str,
        head_sha: str,
    ) -> tuple[tuple[tuple[int, int, str, str | None, str], ...], bool]:
        raw_runs, complete = self._paged_items(
            client,
            f"{base_url}/actions/runs",
            params={"head_sha": head_sha, "event": "pull_request"},
            payload_key="workflow_runs",
            max_pages=10,
        )
        signature: list[tuple[int, int, str, str | None, str]] = []
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
            signature.append((run_id, attempt, status, conclusion, run_head_sha))
        return tuple(sorted(signature)), complete

    @staticmethod
    def _workflow_signature(
        workflows: tuple[ReviewWorkflowEvidence, ...],
    ) -> tuple[tuple[int, int, str, str | None, str], ...]:
        return tuple(
            sorted(
                (
                    item.run_id,
                    item.attempt,
                    item.status,
                    item.conclusion,
                    item.head_sha,
                )
                for item in workflows
            )
        )

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
        base = payload.get("base")
        if base is not None and not isinstance(base, dict):
            raise ExecutionPayloadError("GitHub pull-request base must be object or null")
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
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        url = payload.get("html_url")
        mergeable = payload.get("mergeable")
        auto_merge = payload.get("auto_merge")
        mergeable_state = payload.get("mergeable_state")
        if mergeable is not None and not isinstance(mergeable, bool):
            mergeable = None
        if mergeable_state is not None and not isinstance(mergeable_state, str):
            mergeable_state = None
        if auto_merge is not None and not isinstance(auto_merge, dict):
            raise ExecutionPayloadError("GitHub pull-request auto_merge must be object or null")
        for value, field in (
            (merged_at, "merged_at"),
            (created_at, "created_at"),
            (updated_at, "updated_at"),
            (url, "html_url"),
        ):
            if value is not None and not isinstance(value, str):
                raise ExecutionPayloadError(f"GitHub pull-request {field} must be text or null")
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
            created_at=created_at,
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
