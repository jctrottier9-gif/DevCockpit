"""Read-only GitHub ref revalidation for accepted delivery contracts (ADR-0017).

No branch creation, retargeting, release publication or merge occurs here.
"""
from __future__ import annotations

from urllib.parse import quote
import hashlib

import httpx

from app.domain.delivery_context import (
    DeliveryContext, DeliveryMode, DeliveryObservation, ReferenceKind, validate_observation,
)


class DeliveryReferenceError(RuntimeError):
    """Missing, moved, ambiguous or unverified GitHub delivery evidence."""


class GitHubDeliveryReferenceReader:
    def __init__(self, *, token: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 timeout_seconds: float = 5.0) -> None:
        self._token = token
        self._transport = transport
        self._timeout = timeout_seconds

    def read_verified(self, context: DeliveryContext, *, pr_number: int | None = None,
                      allow_merged_advance: bool = False) -> DeliveryObservation:
        owner, repo = context.repository_full_name.split("/", 1)
        base = "https://api.github.com/repos/" + quote(owner, safe="") + "/" + quote(repo, safe="")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "DevCockpit"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token

        def get(client: httpx.Client, suffix: str) -> dict:
            try:
                response = client.get(base + suffix)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise DeliveryReferenceError("GitHub returned an invalid object")
                return result
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                raise DeliveryReferenceError("GitHub delivery reference unavailable") from exc

        def branch(client: httpx.Client, name: str) -> str:
            payload = get(client, "/git/ref/heads/" + quote(name, safe="/"))
            obj = payload.get("object")
            if payload.get("ref") != "refs/heads/" + name or not isinstance(obj, dict):
                raise DeliveryReferenceError("Ambiguous branch ref response")
            return commit_sha(obj)

        def commit_sha(obj: dict) -> str:
            sha = obj.get("sha")
            if not isinstance(sha, str) or len(sha) != 40 or any(x not in "0123456789abcdef" for x in sha):
                raise DeliveryReferenceError("GitHub returned a non-full commit SHA")
            if obj.get("type") != "commit":
                raise DeliveryReferenceError("Ref did not resolve to a commit")
            return sha

        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout,
                              headers=headers, follow_redirects=False) as client:
                info = get(client, "")
                if (info.get("id") != context.repository_id
                        or info.get("full_name") != context.repository_full_name):
                    raise DeliveryReferenceError("REPOSITORY_MISMATCH")
                issue = get(client, "/issues/" + str(context.delivery_issue_number))
                body = issue.get("body")
                if not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != context.accepted_issue_body_sha256:
                    raise DeliveryReferenceError("DELIVERY_ISSUE_CHANGED")
                if context.ref_kind is ReferenceKind.BRANCH:
                    source_sha = branch(client, context.requested_ref)
                elif context.ref_kind is ReferenceKind.TAG:
                    tag = get(client, "/git/ref/tags/" + quote(context.requested_ref, safe="/"))
                    if tag.get("ref") != context.resolved_ref or not isinstance(tag.get("object"), dict):
                        raise DeliveryReferenceError("Ambiguous tag ref")
                    obj = tag["object"]
                    visited: set[str] = set()
                    while obj.get("type") == "tag":
                        tag_sha = obj.get("sha")
                        if not isinstance(tag_sha, str) or tag_sha in visited or len(visited) >= 8:
                            raise DeliveryReferenceError("Cyclic or ambiguous annotated tag")
                        visited.add(tag_sha)
                        annotated = get(client, "/git/tags/" + quote(tag_sha, safe=""))
                        obj = annotated.get("object")
                        if not isinstance(obj, dict):
                            raise DeliveryReferenceError("Invalid annotated tag target")
                    source_sha = commit_sha(obj)
                else:
                    # A full SHA is the immutable origin; prove GitHub knows this commit.
                    commit = get(client, "/commits/" + context.source_sha)
                    source_sha = commit.get("sha")
                    if source_sha != context.source_sha:
                        raise DeliveryReferenceError("SHA_COMMIT_MISMATCH")

                base_sha = branch(client, context.expected_pr_base)
                working_sha = None
                # A new WorkItem may not yet have its working branch. A 404 is
                # permitted only before PR creation; all other read errors fail.
                if pr_number is not None and not allow_merged_advance:
                    working_sha = branch(client, context.expected_work_branch)
                elif pr_number is not None:
                    # A merged PR may have had its source branch deleted by GitHub.
                    working_sha = None
                pr_base = None
                merged_advance_proven = False
                if pr_number is not None:
                    pr = get(client, "/pulls/" + str(pr_number))
                    base_ref = pr.get("base")
                    head_ref = pr.get("head")
                    if not isinstance(base_ref, dict) or not isinstance(head_ref, dict):
                        raise DeliveryReferenceError("PR has no identifiable base/head")
                    pr_base = base_ref.get("ref")
                    if head_ref.get("ref") != context.expected_work_branch:
                        raise DeliveryReferenceError("PR_HEAD_MISMATCH")
                    head_repo = head_ref.get("repo")
                    if not isinstance(head_repo, dict) or head_repo.get("id") != context.repository_id:
                        raise DeliveryReferenceError("PR_REPOSITORY_MISMATCH")
                    if (allow_merged_advance and context.mode is DeliveryMode.HOTFIX
                            and pr.get("merged_at") is not None
                            and pr_base == context.expected_pr_base):
                        # A maintained release moves only after a verified merge.
                        # Do not mistake an arbitrary moved base for permission.
                        merged_sha = pr.get("merge_commit_sha")
                        if (not isinstance(merged_sha, str) or len(merged_sha) != 40):
                            raise DeliveryReferenceError("MERGED_COMMIT_MISSING")
                        for start in (context.observed_pr_base_sha, merged_sha):
                            if start == base_sha:
                                continue
                            compare = get(
                                client, "/compare/" + start + "..." + base_sha
                            )
                            if (compare.get("status") != "ahead"
                                    or not isinstance(compare.get("merge_base_commit"), dict)
                                    or compare["merge_base_commit"].get("sha") != start):
                                raise DeliveryReferenceError("RELEASE_ADVANCE_NOT_PROVEN")
                        historical = get(client, "/commits/" + context.source_sha)
                        if historical.get("sha") != context.source_sha:
                            raise DeliveryReferenceError("HISTORICAL_COMMIT_MISSING")
                        merged_advance_proven = True
                obs = DeliveryObservation(
                    repository_id=info["id"],
                    repository_full_name=info["full_name"],
                    resolved_ref=context.resolved_ref,
                    source_commit_sha=source_sha,
                    current_base_sha=base_sha,
                    working_branch=context.expected_work_branch if working_sha is not None else None,
                    working_head_sha=working_sha,
                    pr_base_name=pr_base,
                )
                diagnostics = validate_observation(context, obs)
                if merged_advance_proven:
                    diagnostics = tuple(
                        code for code in diagnostics
                        if code not in {"SOURCE_MOVED_OR_MISSING", "BASE_TIP_STALE"}
                    )
                if diagnostics:
                    raise DeliveryReferenceError(",".join(diagnostics))
                return obs
        except httpx.RequestError as exc:
            raise DeliveryReferenceError("GitHub delivery reference unavailable") from exc
