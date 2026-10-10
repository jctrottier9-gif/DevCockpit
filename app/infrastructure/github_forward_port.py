"""Verified hotfix provenance and explicit, idempotent forward-port workflow.

The coordinator does not apply arbitrary Git patches. The DEV applies the proven
integrated delta with git cherry-pick -x (or a reviewed manual adaptation) on
the prepared main-based branch. GitHub is re-read before every mutation.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.delivery_context import (
    DeliveryContext, DeliveryMode, DeliveryContractError, validate_pair,
)
from app.domain.project import Project
from app.infrastructure.github_release_workflow import (
    FULL_SHA, GitHubReleaseWorkflow, ReleaseWorkflowError,
)


class ForwardPortError(ReleaseWorkflowError):
    """A forward-port cannot be proven or safely mutated."""


@dataclass(frozen=True, slots=True)
class ForwardPortPreparation:
    branch: str
    main_sha: str
    head_sha: str
    source_pr: int
    source_commits: tuple[str, ...]
    method: str
    action: str


@dataclass(frozen=True, slots=True)
class ForwardPortPullRequest:
    number: int
    head_sha: str
    url: str
    created: bool


class GitHubForwardPortWorkflow(GitHubReleaseWorkflow):
    """A separate WorkItem/PR to main; no release-wide merge and no deployment."""

    def _contracts(self, project: Project, context: DeliveryContext,
                   source: DeliveryContext) -> None:
        try:
            validate_pair(source, context)
        except DeliveryContractError as exc:
            raise ForwardPortError("UNLINKED_FORWARD_PORT") from exc
        if (context.mode is not DeliveryMode.FORWARD_PORT
                or project.delivery_context_for(context.work_item_id) != context
                or project.delivery_context_for(source.work_item_id) != source
                or context.source_hotfix_pr is None
                or context.repository_full_name != source.repository_full_name
                or context.expected_pr_base != "main"
                or context.expected_work_branch == source.expected_work_branch
                or context.starting_sha != context.observed_pr_base_sha
                or context.starting_sha != context.source_sha):
            raise ForwardPortError("FORWARD_PORT_CONTRACT_MISMATCH")
        if context.forward_port_method not in {
            "CHERRY_PICK_INTEGRATED", "CHERRY_PICK_REBASE", "MERGE_DELTA_ADAPT",
        }:
            raise ForwardPortError("UNSUPPORTED_FORWARD_PORT_METHOD")

    def _integrated_source(self, client, root: str, project: Project,
                           context: DeliveryContext, source: DeliveryContext) -> None:
        # Revalidate the hotfix's immutable issue and historical release lineage.
        self._anchor(client, source, project, after_merge=True)
        pr = self._get(client, root + "/pulls/" + str(context.source_hotfix_pr))
        if not isinstance(pr, dict) or not pr.get("merged_at"):
            raise ForwardPortError("SOURCE_HOTFIX_NOT_MERGED")
        head, base = pr.get("head"), pr.get("base")
        body = pr.get("body")
        if (not isinstance(head, dict) or not isinstance(base, dict)
                or not isinstance(body, str)
                or head.get("ref") != source.expected_work_branch
                or (head.get("repo") or {}).get("id") != source.repository_id
                or base.get("ref") != source.release_branch
                or "Work-Item: " + source.work_item_id not in body.splitlines()
                or "Delivery-Context-SHA256: " + source.fingerprint() not in body.splitlines()
                or "Correction-Id: " + context.correction_id not in body.splitlines()):
            raise ForwardPortError("SOURCE_HOTFIX_IDENTITY_MISMATCH")
        integrated = pr.get("merge_commit_sha")
        if not isinstance(integrated, str) or not FULL_SHA.fullmatch(integrated):
            raise ForwardPortError("SOURCE_MERGE_SHA_MISSING")
        if context.integrated_commits[-1] != integrated:
            raise ForwardPortError("INTEGRATED_COMMIT_MISMATCH")
        if not self._descendant(
            client, root, integrated,
            self._branch(client, root, source.release_branch),
        ):
            raise ForwardPortError("INTEGRATED_COMMIT_NOT_IN_RELEASE")
        git_commit = self._get(client, root + "/commits/" + integrated)
        parents = git_commit.get("parents") if isinstance(git_commit, dict) else None
        if not isinstance(parents, list):
            raise ForwardPortError("INTEGRATED_PARENTS_UNAVAILABLE")
        method = context.forward_port_method
        if method == "MERGE_DELTA_ADAPT":
            if (len(parents) != 2 or len(context.integrated_commits) != 1
                    or parents[0].get("sha") != source.starting_sha):
                raise ForwardPortError("MERGE_DELTA_PARENT_UNPROVEN")
            # The release-relative *first-parent* diff includes merge resolutions.
            self._get(client, root + "/compare/" + source.starting_sha + "..." + integrated)
        else:
            if len(parents) != 1:
                raise ForwardPortError("MERGE_COMMIT_NOT_CHERRY_PICKABLE")
            if method == "CHERRY_PICK_INTEGRATED":
                if (len(context.integrated_commits) != 1
                        or parents[0].get("sha") != source.starting_sha):
                    raise ForwardPortError("SQUASH_OR_INTEGRATED_PARENT_MISMATCH")
            else:
                # Rebase commits must be the complete contiguous first-parent
                # chain actually integrated into the maintained release.
                previous = source.starting_sha
                for sha in context.integrated_commits:
                    record = self._get(client, root + "/commits/" + sha)
                    lineage = record.get("parents") if isinstance(record, dict) else None
                    if (not isinstance(lineage, list) or len(lineage) != 1
                            or lineage[0].get("sha") != previous):
                        raise ForwardPortError("REBASE_INTEGRATED_CHAIN_MISMATCH")
                    previous = sha
        # The requested source can never be a pre-squash PR head substitute.
        if not self._descendant(client, root, source.starting_sha, integrated):
            raise ForwardPortError("SOURCE_INTEGRATED_LINEAGE_MISMATCH")

    def _verified(self, client, project: Project, context: DeliveryContext,
                  source: DeliveryContext) -> tuple[str, str]:
        self._contracts(project, context, source)
        # _anchor checks repository, accepted issue hash, exact source and main SHA.
        root = self._anchor(client, context, project)
        self._integrated_source(client, root, project, context, source)
        main_sha = self._branch(client, root, "main")
        if main_sha != context.observed_pr_base_sha:
            raise ForwardPortError("MAIN_BASE_STALE_REACCEPT_REQUIRED")
        for sha in context.integrated_commits:
            if self._descendant(client, root, sha, main_sha):
                raise ForwardPortError("ALREADY_PRESENT_EQUIVALENCE_DECISION_REQUIRED")
        return root, main_sha

    def prepare_forward_port(self, project: Project, context: DeliveryContext,
                             source: DeliveryContext) -> ForwardPortPreparation:
        with self._client() as client:
            root, main = self._verified(client, project, context, source)
            existing = self._branch(client, root, context.expected_work_branch,
                                    allow_missing=True)
            if existing is None:
                existing = self._create_branch(
                    client, root, context.expected_work_branch, main,
                )
            if not self._descendant(client, root, context.starting_sha, existing):
                raise ForwardPortError("FORWARD_BRANCH_LINEAGE_MISMATCH")
            action = ("APPLY_INTEGRATED_DELTA"
                      if existing == main else "REVIEW_EXISTING_FORWARD_PORT")
            return ForwardPortPreparation(
                context.expected_work_branch, main, existing,
                context.source_hotfix_pr or 0, context.integrated_commits,
                context.forward_port_method, action,
            )

    def ensure_forward_port_pr(self, project: Project, context: DeliveryContext,
                               source: DeliveryContext, *,
                               title: str, expected_head_sha: str,
                               description: str = "") -> ForwardPortPullRequest:
        if not title.startswith(context.work_item_id + " "):
            raise ForwardPortError("WORKITEM_TITLE_REQUIRED")
        if not FULL_SHA.fullmatch(expected_head_sha):
            raise ForwardPortError("FULL_HEAD_SHA_REQUIRED")
        with self._client() as client:
            root, main = self._verified(client, project, context, source)
            # A main-targeted forward-port is reviewed and checked independently.
            self.require_release_protection(client, root, "main")
            head = self._branch(client, root, context.expected_work_branch)
            if head != expected_head_sha:
                raise ForwardPortError("FORWARD_HEAD_STALE")
            if not self._descendant(client, root, context.starting_sha, head):
                raise ForwardPortError("FORWARD_HEAD_LINEAGE_MISMATCH")
            changes = self._get(client, root + "/compare/" + main + "..." + head)
            if (not isinstance(changes, dict) or changes.get("status") != "ahead"
                    or not isinstance(changes.get("ahead_by"), int)
                    or changes["ahead_by"] < 1 or changes.get("behind_by") != 0
                    or not isinstance(changes.get("commits"), list)
                    or changes.get("total_commits") != len(changes["commits"])):
                raise ForwardPortError("FORWARD_DIFF_STALE_EMPTY_OR_INCOMPLETE")
            messages = []
            for entry in changes["commits"]:
                info = entry.get("commit") if isinstance(entry, dict) else None
                message = info.get("message") if isinstance(info, dict) else None
                if not isinstance(message, str):
                    raise ForwardPortError("FORWARD_COMMIT_PROVENANCE_MISSING")
                messages.append(message)
            for source_sha in context.integrated_commits:
                cherry = "(cherry picked from commit " + source_sha + ")"
                adaptation = "Forward-Port-Of: " + source_sha
                def documented_adaptation(message: str) -> bool:
                    lines = message.splitlines()
                    return (adaptation in lines
                            and any(line.startswith("Forward-Port-Reason: ")
                                    and line.removeprefix("Forward-Port-Reason: ").strip()
                                    for line in lines))
                # An integrated merge needs a reviewed release-relative delta.
                # Never authorize an arbitrary mainline cherry-pick of the merge.
                if context.forward_port_method == "MERGE_DELTA_ADAPT":
                    proven = any(documented_adaptation(message) for message in messages)
                else:
                    proven = any(cherry in message or documented_adaptation(message)
                                 for message in messages)
                if not proven:
                    raise ForwardPortError("FORWARD_PATCH_PROVENANCE_MISSING")
            prs = self._matching_prs(client, root, context)
            if len(prs) > 1:
                raise ForwardPortError("AMBIGUOUS_FORWARD_PORT_PR")
            marker = "Delivery-Context-SHA256: " + context.fingerprint()
            if prs:
                pr = prs[0]
                pr_head, target = pr.get("head"), pr.get("base")
                body = pr.get("body") or ""
                if (not isinstance(pr_head, dict) or not isinstance(target, dict)
                        or pr_head.get("ref") != context.expected_work_branch
                        or pr_head.get("sha") != head
                        or (pr_head.get("repo") or {}).get("id") != context.repository_id
                        or target.get("ref") != "main"
                        or marker not in body.splitlines()
                        or "Work-Item: " + context.work_item_id not in body.splitlines()
                        or "Source-Hotfix-PR: #" + str(context.source_hotfix_pr) not in body.splitlines()
                        or "Integrated-Commits: " + ",".join(context.integrated_commits) not in body.splitlines()):
                    raise ForwardPortError("FORWARD_PR_IDENTITY_MISMATCH")
                return ForwardPortPullRequest(
                    pr["number"], head, str(pr.get("html_url") or ""), False,
                )
            body = (
                "Work-Item: " + context.work_item_id + "\n"
                "Delivery-Context-SHA256: " + context.fingerprint() + "\n"
                "Correction-Id: " + context.correction_id + "\n"
                "Source-Hotfix-WorkItem: " + source.work_item_id + "\n"
                "Source-Hotfix-PR: #" + str(context.source_hotfix_pr) + "\n"
                "Integrated-Commits: " + ",".join(context.integrated_commits) + "\n"
                "Forward-Port-Method: " + context.forward_port_method + "\n"
                "Target-Branch: main\n\n" + description +
                "\n\nReview the source-relative integrated delta and any adaptation. "
                "No deployment or implicit migration."
            )
            pr = self._post(client, root + "/pulls", {
                "title": title, "head": context.expected_work_branch,
                "base": "main", "body": body,
            })
            if not isinstance(pr, dict) or type(pr.get("number")) is not int:
                raise ForwardPortError("INVALID_CREATED_FORWARD_PR")
            return ForwardPortPullRequest(
                pr["number"], head, str(pr.get("html_url") or ""), True,
            )
