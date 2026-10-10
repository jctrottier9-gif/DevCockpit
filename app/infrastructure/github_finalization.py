from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.pr_finalization import FinalizationMutationResult
from app.domain.pr_finalization import FinalizationAttemptStatus
from app.domain.project import Project
from app.domain.delivery_context import DeliveryContext, DeliveryMode
from app.infrastructure.github_delivery_context import GitHubDeliveryReferenceReader, DeliveryReferenceError
from app.infrastructure.github_release_workflow import GitHubReleaseWorkflow, ReleaseWorkflowError


_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_RUNNING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


class GitHubPullRequestFinalizer:
    """Guarded GitHub mutations for branch synchronization and PR merge."""

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

    def sync_branch(
        self,
        project: Project,
        *,
        pr_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
        delivery_context: DeliveryContext | None = None,
    ) -> FinalizationMutationResult:
        base_url, headers = self._connection(project)
        try:
            with self._client(headers) as client:
                detail = self._request_json(client, f"{base_url}/pulls/{pr_number}")
                stale = self._validate_open_identity(
                    detail,
                    expected_head_sha=expected_head_sha,
                )
                if stale is not None:
                    return stale
                base_stale = self._validate_current_base(
                    client,
                    base_url,
                    detail,
                    expected_base_sha=expected_base_sha,
                )
                if base_stale is not None:
                    return base_stale
                safety = self._guard_release(
                    client, project, base_url, detail, pr_number,
                    expected_head_sha, delivery_context, for_merge=False,
                )
                if safety is not None:
                    return safety

                response = client.put(
                    f"{base_url}/pulls/{pr_number}/update-branch",
                    json={"expected_head_sha": expected_head_sha},
                )
                if response.status_code == 202:
                    payload = self._json_or_empty(response)
                    message = payload.get("message") if isinstance(payload, dict) else None
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.SUCCEEDED,
                        message=message if isinstance(message, str) else "GitHub accepted branch synchronization.",
                    )
                return self._blocked_response(
                    response,
                    code="BRANCH_SYNC_BLOCKED",
                    conflict_requires_dev=True,
                )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.BLOCKED,
                error_code="GITHUB_UNAVAILABLE",
                message=str(exc),
            )

    def merge_pull_request(
        self,
        project: Project,
        *,
        pr_number: int,
        expected_head_sha: str,
        expected_base_sha: str | None,
        delivery_context: DeliveryContext | None = None,
    ) -> FinalizationMutationResult:
        base_url, headers = self._connection(project)
        try:
            with self._client(headers) as client:
                repository = self._request_json(client, base_url)
                detail = self._request_json(client, f"{base_url}/pulls/{pr_number}")
                stale = self._validate_open_identity(
                    detail,
                    expected_head_sha=expected_head_sha,
                )
                if stale is not None:
                    return stale
                base_stale = self._validate_current_base(
                    client,
                    base_url,
                    detail,
                    expected_base_sha=expected_base_sha,
                )
                if base_stale is not None:
                    return base_stale
                safety = self._guard_release(
                    client, project, base_url, detail, pr_number,
                    expected_head_sha, delivery_context, for_merge=True,
                )
                if safety is not None:
                    return safety

                mergeable = detail.get("mergeable") if isinstance(detail, dict) else None
                mergeable_state = detail.get("mergeable_state") if isinstance(detail, dict) else None
                if mergeable_state == "behind":
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.STALE,
                        error_code="BASE_OUTDATED",
                        message="Pull-request base advanced before merge finalization.",
                    )
                if mergeable is not True or mergeable_state in {"dirty", "blocked"}:
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.BLOCKED,
                        error_code="MERGE_BLOCKED",
                        message=f"GitHub mergeability is {mergeable_state or mergeable!r}.",
                    )

                if not self._current_head_ci_green(client, base_url, expected_head_sha):
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.STALE,
                        error_code="CI_NOT_GREEN",
                        message="Current-head pull-request workflows are no longer completely green.",
                    )

                method = self._merge_method(repository)
                if method is None:
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.BLOCKED,
                        error_code="MERGE_METHOD_UNAVAILABLE",
                        message="Repository configuration exposes no permitted merge method.",
                    )

                response = client.put(
                    f"{base_url}/pulls/{pr_number}/merge",
                    json={
                        "sha": expected_head_sha,
                        "merge_method": method,
                    },
                )
                payload = self._json_or_empty(response)
                if response.status_code == 200 and isinstance(payload, dict) and payload.get("merged") is True:
                    sha = payload.get("sha")
                    return FinalizationMutationResult(
                        FinalizationAttemptStatus.SUCCEEDED,
                        message=(
                            payload.get("message")
                            if isinstance(payload.get("message"), str)
                            else "GitHub merged the pull request."
                        ),
                        resulting_head_sha=sha if isinstance(sha, str) else None,
                    )
                return self._blocked_response(
                    response,
                    code="MERGE_BLOCKED",
                    conflict_requires_dev=False,
                    payload=payload,
                )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.BLOCKED,
                error_code="GITHUB_UNAVAILABLE",
                message=str(exc),
            )


    def _guard_release(
        self,
        client: httpx.Client,
        project: Project,
        base_url: str,
        detail: object,
        pr_number: int,
        expected_head_sha: str,
        context: DeliveryContext | None,
        *,
        for_merge: bool,
    ) -> FinalizationMutationResult | None:
        """Recheck an accepted hotfix target and its protected CI before GitHub write."""
        if context is None:
            return None
        if (context.mode is not DeliveryMode.HOTFIX
                or project.delivery_context_for(context.work_item_id) != context):
            return FinalizationMutationResult(
                FinalizationAttemptStatus.BLOCKED,
                error_code="UNSUPPORTED_RELEASE_FINALIZATION",
                message="Only an accepted HOTFIX delivery is finalizable in DC-075B.",
            )
        head = detail.get("head") if isinstance(detail, dict) else None
        base = detail.get("base") if isinstance(detail, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        if (not isinstance(head, dict) or not isinstance(base, dict)
                or head.get("ref") != context.expected_work_branch
                or not isinstance(head_repo, dict)
                or head_repo.get("id") != context.repository_id
                or base.get("ref") != context.expected_pr_base):
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="RELEASE_PR_TARGET_MISMATCH",
                message="GitHub PR head/repository/base differs from accepted hotfix delivery.",
            )
        try:
            GitHubDeliveryReferenceReader(
                token=self._token, transport=self._transport,
                timeout_seconds=self._timeout_seconds,
            ).read_verified(context, pr_number=pr_number)
            workflow = GitHubReleaseWorkflow(
                token=self._token, transport=self._transport,
                timeout_seconds=self._timeout_seconds,
            )
            required = workflow.require_release_protection(
                client, base_url, context.release_branch,
            )
            if for_merge:
                workflow.verify_release_checks(
                    client, base_url, expected_head_sha, required,
                )
        except (DeliveryReferenceError, ReleaseWorkflowError) as exc:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.BLOCKED,
                error_code="RELEASE_SAFETY_BLOCKED",
                message=str(exc),
            )
        return None

    def _connection(self, project: Project) -> tuple[str, dict[str, str]]:
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
        return base_url, headers

    def _client(self, headers: dict[str, str]) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers=headers,
        )

    def _request_json(self, client: httpx.Client, url: str, **kwargs) -> object:
        response = client.get(url, **kwargs)
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"GitHub request failed with HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        return self._json_or_empty(response)

    @staticmethod
    def _json_or_empty(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _validate_open_identity(
        detail: object,
        *,
        expected_head_sha: str,
    ) -> FinalizationMutationResult | None:
        if not isinstance(detail, dict):
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="PR_EVIDENCE_INVALID",
                message="Pull-request detail is not an object.",
            )
        if detail.get("merged_at") is not None:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.SUCCEEDED,
                message="Pull request is already merged.",
            )
        if detail.get("state") != "open":
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="PR_NOT_OPEN",
                message="Pull request is no longer open.",
            )
        head = detail.get("head")
        current_head = head.get("sha") if isinstance(head, dict) else None
        if current_head != expected_head_sha:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="HEAD_MOVED",
                message="Pull-request head changed before mutation.",
            )
        return None

    def _validate_current_base(
        self,
        client: httpx.Client,
        base_url: str,
        detail: object,
        *,
        expected_base_sha: str | None,
    ) -> FinalizationMutationResult | None:
        if expected_base_sha is None:
            return None
        if not isinstance(detail, dict):
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="PR_EVIDENCE_INVALID",
                message="Pull-request detail is not an object.",
            )
        base = detail.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        if not isinstance(base_ref, str) or not base_ref:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="BASE_REF_MISSING",
                message="Pull-request base branch is unavailable before mutation.",
            )
        payload = self._request_json(
            client,
            f"{base_url}/branches/{quote(base_ref, safe='')}",
        )
        if not isinstance(payload, dict):
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="BASE_EVIDENCE_INVALID",
                message="Current base branch evidence is invalid.",
            )
        commit = payload.get("commit")
        current_base_sha = commit.get("sha") if isinstance(commit, dict) else None
        if current_base_sha != expected_base_sha:
            return FinalizationMutationResult(
                FinalizationAttemptStatus.STALE,
                error_code="BASE_MOVED",
                message="Current base branch tip changed before mutation.",
            )
        return None

    def _current_head_ci_green(
        self,
        client: httpx.Client,
        base_url: str,
        head_sha: str,
    ) -> bool:
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
            return False
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            return False
        current = [
            item
            for item in runs
            if isinstance(item, dict) and item.get("head_sha") == head_sha
        ]
        if not current:
            return False
        if any(item.get("status") in _RUNNING_STATUSES for item in current):
            return False
        return all(
            item.get("status") == "completed"
            and item.get("conclusion") in _GREEN_CONCLUSIONS
            for item in current
        )

    @staticmethod
    def _merge_method(repository: object) -> str | None:
        if not isinstance(repository, dict):
            return None
        for field, method in (
            ("allow_merge_commit", "merge"),
            ("allow_squash_merge", "squash"),
            ("allow_rebase_merge", "rebase"),
        ):
            if repository.get(field) is True:
                return method
        return None

    def _blocked_response(
        self,
        response: httpx.Response,
        *,
        code: str,
        conflict_requires_dev: bool,
        payload: object | None = None,
    ) -> FinalizationMutationResult:
        body = payload if payload is not None else self._json_or_empty(response)
        message = body.get("message") if isinstance(body, dict) else None
        details: list[str] = []
        if isinstance(message, str):
            details.append(message)
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list):
                for error in errors:
                    if isinstance(error, str):
                        details.append(error)
                    elif isinstance(error, dict):
                        for field in ("message", "code"):
                            value = error.get(field)
                            if isinstance(value, str):
                                details.append(value)
        text = " · ".join(dict.fromkeys(details)) or f"GitHub HTTP {response.status_code}"
        lower = text.lower()
        requires_dev = (
            conflict_requires_dev
            and response.status_code in {409, 422}
            and (
                "conflict" in lower
                or "cannot be merged" in lower
                or "cannot be cleanly" in lower
            )
        )
        return FinalizationMutationResult(
            FinalizationAttemptStatus.BLOCKED,
            error_code=code,
            message=text,
            requires_dev=requires_dev,
        )
