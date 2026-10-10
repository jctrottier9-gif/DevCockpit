"""Guarded release/hotfix GitHub workflow (ADR-0017 / DC-075B).

Only an accepted immutable DeliveryContext can authorize these explicit operations.
No forward-port, deployment, schema migration or automatic tag publishing lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from urllib.parse import quote

import httpx

from app.domain.delivery_context import DeliveryContext, DeliveryMode, ReferenceKind
from app.domain.project import Project


FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ReleaseWorkflowError(RuntimeError):
    """GitHub or accepted context does not authorize a release mutation."""


@dataclass(frozen=True, slots=True)
class HotfixPullRequest:
    number: int
    head_sha: str
    base: str
    url: str
    created: bool


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    tag: str
    source_sha: str
    image: str
    digest: str
    validation_check: str


class GitHubReleaseWorkflow:
    def __init__(self, *, token: str | None = None,
                 timeout_seconds: float = 5.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "DevCockpit"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return httpx.Client(transport=self.transport, timeout=self.timeout_seconds,
                            headers=headers, follow_redirects=False)

    @staticmethod
    def _root(context: DeliveryContext) -> str:
        owner, repo = context.repository_full_name.split("/", 1)
        return ("https://api.github.com/repos/" + quote(owner, safe="")
                + "/" + quote(repo, safe=""))

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise ReleaseWorkflowError("INVALID_GITHUB_JSON") from exc

    def _get(self, client: httpx.Client, url: str, *,
             allow_missing: bool = False, params: dict | None = None) -> object | None:
        try:
            response = client.get(url, params=params)
        except httpx.RequestError as exc:
            raise ReleaseWorkflowError("GITHUB_UNAVAILABLE") from exc
        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code != 200:
            raise ReleaseWorkflowError(f"GITHUB_HTTP_{response.status_code}")
        return self._json(response)

    def _post(self, client: httpx.Client, url: str, payload: dict) -> object:
        try:
            response = client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise ReleaseWorkflowError("GITHUB_MUTATION_UNCERTAIN_REOBSERVE") from exc
        if response.status_code not in {200, 201}:
            raise ReleaseWorkflowError(f"GITHUB_MUTATION_HTTP_{response.status_code}_REOBSERVE")
        return self._json(response)

    def _branch(self, client: httpx.Client, root: str, name: str,
                *, allow_missing: bool = False) -> str | None:
        result = self._get(client, root + "/git/ref/heads/" + quote(name, safe="/"),
                           allow_missing=allow_missing)
        if result is None:
            return None
        if not isinstance(result, dict) or result.get("ref") != "refs/heads/" + name:
            raise ReleaseWorkflowError("AMBIGUOUS_BRANCH")
        obj = result.get("object")
        if not isinstance(obj, dict) or obj.get("type") != "commit" or not FULL_SHA.fullmatch(str(obj.get("sha"))):
            raise ReleaseWorkflowError("INVALID_BRANCH_COMMIT")
        return obj["sha"]

    def _source(self, client: httpx.Client, root: str, context: DeliveryContext) -> str:
        if context.ref_kind is ReferenceKind.BRANCH:
            return self._branch(client, root, context.requested_ref) or ""
        if context.ref_kind is ReferenceKind.SHA:
            data = self._get(client, root + "/commits/" + context.source_sha)
            if not isinstance(data, dict) or data.get("sha") != context.source_sha:
                raise ReleaseWorkflowError("COMMIT_MISMATCH")
            return context.source_sha
        tag = self._get(client, root + "/git/ref/tags/" + quote(context.requested_ref, safe="/"))
        if not isinstance(tag, dict) or tag.get("ref") != context.resolved_ref:
            raise ReleaseWorkflowError("TAG_MISSING_OR_MOVED")
        obj = tag.get("object")
        seen: set[str] = set()
        for _ in range(8):
            if not isinstance(obj, dict):
                raise ReleaseWorkflowError("INVALID_TAG_TARGET")
            if obj.get("type") == "commit":
                sha = obj.get("sha")
                if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
                    raise ReleaseWorkflowError("INVALID_TAG_COMMIT")
                return sha
            if obj.get("type") != "tag" or obj.get("sha") in seen:
                raise ReleaseWorkflowError("CYCLIC_OR_INVALID_TAG")
            sha = obj.get("sha")
            if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
                raise ReleaseWorkflowError("INVALID_ANNOTATED_TAG")
            seen.add(sha)
            data = self._get(client, root + "/git/tags/" + sha)
            obj = data.get("object") if isinstance(data, dict) else None
        raise ReleaseWorkflowError("TAG_DEPTH_EXCEEDED")

    def _anchor(self, client: httpx.Client, context: DeliveryContext,
                project: Project) -> str:
        if (project.repository_full_name != context.repository_full_name
                or project.delivery_context_for(context.work_item_id) != context):
            raise ReleaseWorkflowError("UNACCEPTED_DELIVERY_CONTEXT")
        root = self._root(context)
        repo = self._get(client, root)
        if (not isinstance(repo, dict) or repo.get("id") != context.repository_id
                or repo.get("full_name") != context.repository_full_name):
            raise ReleaseWorkflowError("REPOSITORY_MISMATCH")
        issue = self._get(client, root + "/issues/" + str(context.delivery_issue_number))
        body = issue.get("body") if isinstance(issue, dict) else None
        if not isinstance(body, str) or sha256(body.encode()).hexdigest() != context.accepted_issue_body_sha256:
            raise ReleaseWorkflowError("ACCEPTED_ISSUE_CHANGED")
        if self._source(client, root, context) != context.source_sha:
            raise ReleaseWorkflowError("SOURCE_REF_MOVED")
        return root

    def _descendant(self, client: httpx.Client, root: str,
                    ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        compare = self._get(client, root + "/compare/" + ancestor + "..." + descendant)
        return (
            isinstance(compare, dict)
            and compare.get("status") == "ahead"
            and isinstance(compare.get("ahead_by"), int)
            and compare["ahead_by"] > 0
            and isinstance(compare.get("merge_base_commit"), dict)
            and compare["merge_base_commit"].get("sha") == ancestor
        )

    def _create_branch(self, client: httpx.Client, root: str,
                       name: str, origin: str) -> str:
        try:
            self._post(client, root + "/git/refs",
                       {"ref": "refs/heads/" + name, "sha": origin})
        except ReleaseWorkflowError:
            # GitHub timeouts and 422s are ambiguous. Never blindly retry a write.
            observed = self._branch(client, root, name, allow_missing=True)
            if observed != origin:
                raise
        observed = self._branch(client, root, name)
        if observed != origin:
            raise ReleaseWorkflowError("CREATED_BRANCH_MOVED")
        return observed

    def prepare_release(self, project: Project, context: DeliveryContext) -> str:
        """Create or prove a maintained release branch from its frozen origin."""
        if context.mode is not DeliveryMode.RELEASE or context.release_state != "MAINTAINED":
            raise ReleaseWorkflowError("RELEASE_CONTEXT_REQUIRED")
        if context.source_sha != context.release_origin_sha:
            raise ReleaseWorkflowError("RELEASE_ORIGIN_MISMATCH")
        with self._client() as client:
            root = self._anchor(client, context, project)
            existing = self._branch(client, root, context.release_branch, allow_missing=True)
            if existing is None:
                return self._create_branch(client, root, context.release_branch,
                                           context.release_origin_sha)
            if not self._descendant(client, root, context.release_origin_sha, existing):
                raise ReleaseWorkflowError("RELEASE_PROVENANCE_MISMATCH")
            return existing

    def prepare_hotfix(self, project: Project, context: DeliveryContext) -> str:
        """Idempotent exact hotfix branch from accepted release tip, never main."""
        if context.mode is not DeliveryMode.HOTFIX or context.release_state != "MAINTAINED":
            raise ReleaseWorkflowError("MAINTAINED_HOTFIX_CONTEXT_REQUIRED")
        if not (context.starting_sha == context.source_sha == context.observed_pr_base_sha):
            raise ReleaseWorkflowError("HOTFIX_START_AND_BASE_MISMATCH")
        with self._client() as client:
            root = self._anchor(client, context, project)
            release_sha = self._branch(client, root, context.release_branch)
            if release_sha != context.observed_pr_base_sha:
                raise ReleaseWorkflowError("RELEASE_BASE_MOVED")
            if not self._descendant(client, root, context.release_origin_sha, release_sha):
                raise ReleaseWorkflowError("RELEASE_PROVENANCE_MISMATCH")
            existing = self._branch(client, root, context.expected_work_branch,
                                    allow_missing=True)
            if existing is None:
                return self._create_branch(client, root, context.expected_work_branch,
                                           context.starting_sha)
            if not self._descendant(client, root, context.starting_sha, existing):
                raise ReleaseWorkflowError("HOTFIX_BRANCH_NOT_DESCENDANT")
            return existing

    def _matching_prs(self, client: httpx.Client, root: str,
                      context: DeliveryContext) -> list[dict]:
        selected: list[dict] = []
        page = 1
        while True:
            payload = self._get(client, root + "/pulls",
                                params={"state": "all", "per_page": 100, "page": page})
            if not isinstance(payload, list):
                raise ReleaseWorkflowError("INVALID_PR_COLLECTION")
            for pr in payload:
                if not isinstance(pr, dict):
                    raise ReleaseWorkflowError("INVALID_PR")
                head = pr.get("head")
                body = pr.get("body") or ""
                title = pr.get("title") or ""
                branch = head.get("ref") if isinstance(head, dict) else None
                identity = "Work-Item: " + context.work_item_id
                if (branch == context.expected_work_branch or
                    identity in body.splitlines() or
                    title.startswith(context.work_item_id + " ")):
                    selected.append(pr)
            if len(payload) < 100:
                break
            page += 1
            if page > 100:
                raise ReleaseWorkflowError("PR_PAGINATION_INCOMPLETE")
        return selected

    def ensure_hotfix_pr(self, project: Project, context: DeliveryContext,
                         *, title: str, description: str = "") -> HotfixPullRequest:
        if context.mode is not DeliveryMode.HOTFIX:
            raise ReleaseWorkflowError("HOTFIX_CONTEXT_REQUIRED")
        if not title.startswith(context.work_item_id + " "):
            raise ReleaseWorkflowError("WORKITEM_TITLE_REQUIRED")
        with self._client() as client:
            root = self._anchor(client, context, project)
            base = self._branch(client, root, context.release_branch)
            if base != context.observed_pr_base_sha:
                raise ReleaseWorkflowError("RELEASE_BASE_MOVED")
            head_sha = self._branch(client, root, context.expected_work_branch)
            if not self._descendant(client, root, context.starting_sha, head_sha):
                raise ReleaseWorkflowError("HOTFIX_LINEAGE_MISMATCH")
            prs = self._matching_prs(client, root, context)
            if len(prs) > 1:
                raise ReleaseWorkflowError("AMBIGUOUS_HOTFIX_PR")
            if prs:
                pr = prs[0]
                head, target = pr.get("head"), pr.get("base")
                body = pr.get("body") or ""
                if (not isinstance(head, dict) or not isinstance(target, dict)
                        or head.get("ref") != context.expected_work_branch
                        or head.get("sha") != head_sha
                        or (head.get("repo") or {}).get("id") != context.repository_id
                        or target.get("ref") != context.expected_pr_base
                        or "Delivery-Context-SHA256: " + context.fingerprint() not in body.splitlines()
                        or "Work-Item: " + context.work_item_id not in body.splitlines()):
                    raise ReleaseWorkflowError("HOTFIX_PR_IDENTITY_MISMATCH")
                return HotfixPullRequest(pr["number"], head_sha, context.release_branch,
                                         str(pr.get("html_url") or ""), False)
            if head_sha == base:
                raise ReleaseWorkflowError("HOTFIX_HAS_NO_CHANGES")
            if not self._descendant(client, root, base, head_sha):
                raise ReleaseWorkflowError("HOTFIX_NOT_BASED_ON_RELEASE")
            body = ("Work-Item: " + context.work_item_id + "\n"
                    + "Delivery-Context-SHA256: " + context.fingerprint() + "\n"
                    + "Correction-Id: " + context.correction_id + "\n"
                    + "Target-Release: " + context.release_branch + "\n\n"
                    + description + "\n\nNo implicit deployment or database migration.")
            try:
                result = self._post(client, root + "/pulls", {
                    "title": title, "head": context.expected_work_branch,
                    "base": context.release_branch, "body": body,
                })
            except ReleaseWorkflowError:
                # Fail closed on ambiguous writes: subsequent invocation rediscovers PR.
                raise
            if not isinstance(result, dict) or not isinstance(result.get("number"), int):
                raise ReleaseWorkflowError("INVALID_CREATED_PR")
            return HotfixPullRequest(result["number"], head_sha, context.release_branch,
                                     str(result.get("html_url") or ""), True)


    def require_release_protection(self, client: httpx.Client, root: str,
                                   branch: str) -> tuple[str, ...]:
        """Target-specific protection gate, never inherited from main."""
        protection = self._get(
            client, root + "/branches/" + quote(branch, safe="/") + "/protection"
        )
        if not isinstance(protection, dict):
            raise ReleaseWorkflowError("RELEASE_PROTECTION_MISSING")
        required = protection.get("required_status_checks")
        reviews = protection.get("required_pull_request_reviews")
        if not isinstance(required, dict) or not isinstance(reviews, dict):
            raise ReleaseWorkflowError("RELEASE_PROTECTION_INCOMPLETE")
        contexts = required.get("contexts") or []
        checks = required.get("checks") or []
        if not isinstance(contexts, list) or not isinstance(checks, list):
            raise ReleaseWorkflowError("INVALID_REQUIRED_CHECKS")
        names = set()
        for value in contexts:
            if not isinstance(value, str) or not value:
                raise ReleaseWorkflowError("INVALID_REQUIRED_CHECKS")
            names.add(value)
        for value in checks:
            if not isinstance(value, dict) or not isinstance(value.get("context"), str):
                raise ReleaseWorkflowError("INVALID_REQUIRED_CHECKS")
            names.add(value["context"])
        if not names or required.get("strict") is not True:
            raise ReleaseWorkflowError("RELEASE_STRICT_CI_REQUIRED")
        if reviews.get("required_approving_review_count", 0) < 1:
            raise ReleaseWorkflowError("RELEASE_REVIEW_REQUIRED")
        return tuple(sorted(names))

    def verify_release_checks(self, client: httpx.Client, root: str,
                              head_sha: str, required: tuple[str, ...]) -> None:
        """Verify all protected contexts on the current head; no skipped checks."""
        payload = self._get(client, root + "/commits/" + head_sha + "/check-runs",
                            params={"per_page": 100})
        if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
            raise ReleaseWorkflowError("CHECK_RUN_EVIDENCE_INVALID")
        if payload.get("total_count", len(payload["check_runs"])) > len(payload["check_runs"]):
            raise ReleaseWorkflowError("CHECK_RUN_PAGINATION_INCOMPLETE")
        checks = {}
        for item in payload["check_runs"]:
            if not isinstance(item, dict):
                raise ReleaseWorkflowError("INVALID_CHECK_RUN")
            if item.get("head_sha") not in (None, head_sha):
                continue
            name = item.get("name")
            if isinstance(name, str):
                checks.setdefault(name, []).append(item)
        status = self._get(client, root + "/commits/" + head_sha + "/status")
        if not isinstance(status, dict) or not isinstance(status.get("statuses"), list):
            raise ReleaseWorkflowError("COMMIT_STATUS_INVALID")
        if status.get("total_count", len(status["statuses"])) > len(status["statuses"]):
            raise ReleaseWorkflowError("COMMIT_STATUS_INCOMPLETE")
        statuses = {}
        for item in status["statuses"]:
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                statuses.setdefault(item["context"], item.get("state"))
        for name in required:
            runs = checks.get(name, [])
            if runs:
                if len(runs) != 1 or runs[0].get("status") != "completed" or runs[0].get("conclusion") != "success":
                    raise ReleaseWorkflowError("REQUIRED_CHECK_NOT_GREEN:" + name)
            elif statuses.get(name) != "success":
                raise ReleaseWorkflowError("REQUIRED_CHECK_NOT_GREEN:" + name)

    def inspect_artifact(self, project: Project, context: DeliveryContext, *,
                         tag: str, integrated_sha: str,
                         validation_check: str) -> ReleaseArtifact:
        """Read publication evidence; caller must independently verify the registry digest."""
        if context.mode is not DeliveryMode.HOTFIX or not FULL_SHA.fullmatch(integrated_sha):
            raise ReleaseWorkflowError("HOTFIX_PUBLICATION_CONTEXT_REQUIRED")
        if not tag.startswith("v" + context.release_id + ".") or not validation_check:
            raise ReleaseWorkflowError("VERSION_OR_VALIDATION_CHECK_REQUIRED")
        with self._client() as client:
            root = self._anchor(client, context, project)
            ref = self._get(client, root + "/git/ref/tags/" + quote(tag, safe="/"))
            if not isinstance(ref, dict) or ref.get("ref") != "refs/tags/" + tag:
                raise ReleaseWorkflowError("PUBLISHED_TAG_MISSING")
            obj = ref.get("object")
            seen: set[str] = set()
            while isinstance(obj, dict) and obj.get("type") == "tag":
                sha = obj.get("sha")
                if not isinstance(sha, str) or sha in seen or len(seen) >= 8:
                    raise ReleaseWorkflowError("INVALID_PUBLICATION_TAG")
                seen.add(sha)
                annotation = self._get(client, root + "/git/tags/" + sha)
                obj = annotation.get("object") if isinstance(annotation, dict) else None
            if not isinstance(obj, dict) or obj.get("type") != "commit" or obj.get("sha") != integrated_sha:
                raise ReleaseWorkflowError("PUBLISHED_TAG_SOURCE_MISMATCH")
            published = self._get(client, root + "/releases/tags/" + quote(tag, safe=""))
            if not isinstance(published, dict) or published.get("draft") or published.get("tag_name") != tag:
                raise ReleaseWorkflowError("RELEASE_NOT_PUBLISHED")
            body = published.get("body")
            if not isinstance(body, str):
                raise ReleaseWorkflowError("PUBLISHED_DIGEST_MISSING")
            entries = dict(line.split(": ", 1) for line in body.splitlines()
                           if ": " in line and line.split(": ", 1)[0] in
                           {"Source-SHA", "Docker-Image", "Validation-Check"})
            image = entries.get("Docker-Image", "")
            digest = image.rsplit("@", 1)[-1]
            if (entries.get("Source-SHA") != integrated_sha
                    or entries.get("Validation-Check") != validation_check
                    or "@" not in image or not DIGEST.fullmatch(digest)):
                raise ReleaseWorkflowError("PUBLISHED_ARTIFACT_PROVENANCE_INVALID")
            return ReleaseArtifact(tag, integrated_sha, image, digest, validation_check)
