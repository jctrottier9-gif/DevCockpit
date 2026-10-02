from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.flow_analytics import (
    FlowAnalyticsAuthorizationError,
    FlowAnalyticsPayloadError,
    FlowAnalyticsRepositoryNotFoundError,
    FlowAnalyticsSourceError,
)
from app.domain.execution import PullRequestEvidence, WorkflowRunEvidence, pull_request_matches_work_item
from app.domain.flow_analytics import (
    CommitEvidence,
    FlowAnalyticsEvidence,
    FlowDeliveryEvidence,
    FlowDiagnostic,
)
from app.domain.project import Project
from app.domain.roadmap import WorkItem


class GitHubFlowAnalyticsReader:
    """Read-only, on-demand GitHub projection source for delivery-flow analytics."""

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
    ) -> FlowAnalyticsEvidence:
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
                pulls = tuple(
                    self._parse_pull_request(item)
                    for item in self._read_list_pages(
                        client,
                        f"{base_url}/pulls",
                        params={"state": "all", "sort": "updated", "direction": "desc"},
                    )
                )
                deliveries: list[FlowDeliveryEvidence] = []
                diagnostics: list[FlowDiagnostic] = []

                for work_item in work_items:
                    selected, selection_diagnostics = self._select_pull_request(
                        pulls,
                        work_item,
                    )
                    diagnostics.extend(selection_diagnostics)
                    if selected is None:
                        continue
                    deliveries.append(
                        self._read_delivery(
                            client,
                            base_url,
                            work_item,
                            selected,
                        )
                    )

                return FlowAnalyticsEvidence(
                    deliveries=tuple(deliveries),
                    diagnostics=tuple(diagnostics),
                )
        except httpx.RequestError as exc:
            raise FlowAnalyticsSourceError("GitHub analytics request failed") from exc

    def _read_delivery(
        self,
        client: httpx.Client,
        base_url: str,
        work_item: WorkItem,
        pull_request: PullRequestEvidence,
    ) -> FlowDeliveryEvidence:
        diagnostics: list[FlowDiagnostic] = []

        commits_complete = True
        commits: tuple[CommitEvidence, ...] = ()
        try:
            raw_commits = self._read_list_pages(
                client,
                f"{base_url}/pulls/{pull_request.number}/commits",
            )
            parsed_commits: list[CommitEvidence] = []
            for item in raw_commits:
                if not isinstance(item, dict):
                    raise FlowAnalyticsPayloadError("GitHub PR commit entry must be an object")
                sha = self._require_string(item, "sha", context="PR commit")
                commit = item.get("commit")
                committed_at: str | None = None
                if isinstance(commit, dict):
                    committer = commit.get("committer")
                    if isinstance(committer, dict):
                        value = committer.get("date")
                        if value is None or isinstance(value, str):
                            committed_at = value
                parsed_commits.append(CommitEvidence(sha=sha, committed_at=committed_at))
            commits = tuple(parsed_commits)
        except FlowAnalyticsSourceError as exc:
            commits_complete = False
            diagnostics.append(
                FlowDiagnostic(
                    code="COMMIT_HISTORY_UNAVAILABLE",
                    message=f"GitHub PR commit history is unavailable ({exc.code}).",
                    work_item_id=work_item.key,
                    pr_number=pull_request.number,
                )
            )

        changed_files_complete = True
        changed_files: tuple[str, ...] = ()
        try:
            raw_files = self._read_list_pages(
                client,
                f"{base_url}/pulls/{pull_request.number}/files",
            )
            names: list[str] = []
            for item in raw_files:
                if not isinstance(item, dict):
                    raise FlowAnalyticsPayloadError("GitHub PR file entry must be an object")
                names.append(self._require_string(item, "filename", context="PR file"))
            changed_files = tuple(names)
        except FlowAnalyticsSourceError as exc:
            changed_files_complete = False
            diagnostics.append(
                FlowDiagnostic(
                    code="CHANGED_FILES_UNAVAILABLE",
                    message=f"GitHub changed-file evidence is unavailable ({exc.code}).",
                    work_item_id=work_item.key,
                    pr_number=pull_request.number,
                )
            )

        ci_history_complete = True
        workflow_runs: tuple[WorkflowRunEvidence, ...] = ()
        try:
            raw_runs = self._read_object_list_pages(
                client,
                f"{base_url}/actions/runs",
                list_key="workflow_runs",
                params={"event": "pull_request", "branch": pull_request.branch},
            )
            known_shas = {commit.sha for commit in commits}
            known_shas.add(pull_request.head_sha)
            parsed_runs: list[WorkflowRunEvidence] = []
            for item in raw_runs:
                if not isinstance(item, dict):
                    raise FlowAnalyticsPayloadError("GitHub workflow run entry must be an object")
                head_sha = self._require_string(item, "head_sha", context="workflow run")
                if head_sha not in known_shas:
                    continue
                latest = self._parse_workflow_run(item)
                attempts = [latest]
                if latest.attempt > 1:
                    for attempt_number in range(1, latest.attempt):
                        try:
                            payload = self._request_json(
                                client,
                                f"{base_url}/actions/runs/{latest.run_id}/attempts/{attempt_number}",
                            )
                            attempt = self._parse_workflow_run(payload)
                            attempts.append(attempt)
                        except FlowAnalyticsSourceError as exc:
                            ci_history_complete = False
                            diagnostics.append(
                                FlowDiagnostic(
                                    code="CI_ATTEMPT_HISTORY_INCOMPLETE",
                                    message=(
                                        f"Workflow run {latest.run_id} attempt {attempt_number} "
                                        f"could not be read ({exc.code})."
                                    ),
                                    work_item_id=work_item.key,
                                    pr_number=pull_request.number,
                                )
                            )
                parsed_runs.extend(attempts)
            workflow_runs = tuple(
                sorted(
                    {
                        (run.run_id, run.attempt): run
                        for run in parsed_runs
                    }.values(),
                    key=lambda run: (run.run_id, run.attempt),
                )
            )
        except FlowAnalyticsSourceError as exc:
            ci_history_complete = False
            diagnostics.append(
                FlowDiagnostic(
                    code="CI_HISTORY_UNAVAILABLE",
                    message=f"GitHub workflow history is unavailable ({exc.code}).",
                    work_item_id=work_item.key,
                    pr_number=pull_request.number,
                )
            )

        return FlowDeliveryEvidence(
            work_item=work_item,
            pull_request=pull_request,
            commits=commits,
            workflow_runs=workflow_runs,
            changed_files=changed_files,
            commits_complete=commits_complete,
            ci_history_complete=ci_history_complete,
            changed_files_complete=changed_files_complete,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _select_pull_request(
        pulls: tuple[PullRequestEvidence, ...],
        work_item: WorkItem,
    ) -> tuple[PullRequestEvidence | None, tuple[FlowDiagnostic, ...]]:
        matching = tuple(
            item for item in pulls if pull_request_matches_work_item(item, work_item.key)
        )
        if not matching:
            return None, ()
        open_matching = tuple(item for item in matching if item.state == "open" and not item.merged)
        if len(open_matching) > 1:
            return None, (
                FlowDiagnostic(
                    code="AMBIGUOUS_DELIVERY",
                    message=(
                        f"Multiple open pull requests are strongly associated with {work_item.key}; "
                        "analytics fail closed for this WorkItem."
                    ),
                    work_item_id=work_item.key,
                ),
            )
        if len(open_matching) == 1:
            return open_matching[0], ()

        merged = tuple(item for item in matching if item.merged)
        if merged:
            return max(
                merged,
                key=lambda item: (item.merged_at or "", item.updated_at or "", item.number),
            ), ()
        return max(
            matching,
            key=lambda item: (item.updated_at or "", item.number),
        ), ()

    def _read_list_pages(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> list[object]:
        result: list[object] = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"per_page": 100, "page": page})
            payload = self._request_json(client, url, params=query)
            if not isinstance(payload, list):
                raise FlowAnalyticsPayloadError("GitHub paginated response must be an array")
            result.extend(payload)
            if len(payload) < 100:
                return result
            page += 1

    def _read_object_list_pages(
        self,
        client: httpx.Client,
        url: str,
        *,
        list_key: str,
        params: dict[str, object] | None = None,
    ) -> list[object]:
        result: list[object] = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"per_page": 100, "page": page})
            payload = self._request_json(client, url, params=query)
            if not isinstance(payload, dict):
                raise FlowAnalyticsPayloadError("GitHub paginated response must be an object")
            items = payload.get(list_key)
            if not isinstance(items, list):
                raise FlowAnalyticsPayloadError(f"GitHub response {list_key} must be an array")
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1

    def _request_json(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> object:
        try:
            response = client.get(url, params=params)
        except httpx.RequestError as exc:
            raise FlowAnalyticsSourceError("GitHub analytics request failed") from exc
        if response.status_code in {401, 403}:
            raise FlowAnalyticsAuthorizationError("GitHub rejected analytics evidence access")
        if response.status_code == 404:
            raise FlowAnalyticsRepositoryNotFoundError("GitHub analytics resource was not found")
        if response.status_code >= 400:
            raise FlowAnalyticsSourceError(
                f"GitHub analytics request failed with HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise FlowAnalyticsPayloadError("GitHub analytics response was not valid JSON") from exc

    @staticmethod
    def _parse_pull_request(payload: object) -> PullRequestEvidence:
        if not isinstance(payload, dict):
            raise FlowAnalyticsPayloadError("GitHub pull-request entry must be an object")
        head = payload.get("head")
        if not isinstance(head, dict):
            raise FlowAnalyticsPayloadError("GitHub pull-request head must be an object")
        number = payload.get("number")
        if not isinstance(number, int):
            raise FlowAnalyticsPayloadError("GitHub pull-request number must be an integer")
        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise FlowAnalyticsPayloadError("GitHub pull-request body must be text or null")
        return PullRequestEvidence(
            number=number,
            title=GitHubFlowAnalyticsReader._require_string(payload, "title", context="pull request"),
            body=body,
            branch=GitHubFlowAnalyticsReader._require_string(head, "ref", context="pull-request head"),
            head_sha=GitHubFlowAnalyticsReader._require_string(head, "sha", context="pull-request head"),
            state=GitHubFlowAnalyticsReader._require_string(payload, "state", context="pull request"),
            merged=isinstance(payload.get("merged_at"), str),
            mergeable=None,
            url=payload.get("html_url") if isinstance(payload.get("html_url"), str) else None,
            created_at=payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
            updated_at=payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
            merged_at=payload.get("merged_at") if isinstance(payload.get("merged_at"), str) else None,
        )

    @staticmethod
    def _parse_workflow_run(payload: object) -> WorkflowRunEvidence:
        if not isinstance(payload, dict):
            raise FlowAnalyticsPayloadError("GitHub workflow run must be an object")
        run_id = payload.get("id")
        attempt = payload.get("run_attempt", 1)
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            raise FlowAnalyticsPayloadError("GitHub workflow run identity must be integer")
        conclusion = payload.get("conclusion")
        if conclusion is not None and not isinstance(conclusion, str):
            raise FlowAnalyticsPayloadError("GitHub workflow conclusion must be text or null")
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            name = f"workflow-{run_id}"
        return WorkflowRunEvidence(
            run_id=run_id,
            name=name,
            status=GitHubFlowAnalyticsReader._require_string(payload, "status", context="workflow run"),
            conclusion=conclusion,
            attempt=attempt,
            head_sha=GitHubFlowAnalyticsReader._require_string(payload, "head_sha", context="workflow run"),
            url=payload.get("html_url") if isinstance(payload.get("html_url"), str) else None,
            created_at=payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
            updated_at=payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
        )

    @staticmethod
    def _require_string(payload: object, key: str, *, context: str) -> str:
        if not isinstance(payload, dict):
            raise FlowAnalyticsPayloadError(f"GitHub {context} payload must be an object")
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise FlowAnalyticsPayloadError(f"GitHub {context} {key} must be non-empty text")
        return value
