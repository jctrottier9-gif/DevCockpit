"""Accepted, immutable per-WorkItem source/target contract (ADR-0017).

This is a policy/evidence model, not an authorization to perform release writes.
An omitted contract is permitted only for the legacy NORMAL/main path.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import StrEnum
from hashlib import sha256
import json
import re

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^(?!/)(?!.*(?:\.\.|//|@\{|\\|\s))[^~^:?*\[\]]+$")


class DeliveryContractError(ValueError):
    pass


class DeliveryMode(StrEnum):
    NORMAL = "NORMAL"
    RELEASE = "RELEASE"
    HOTFIX = "HOTFIX"
    FORWARD_PORT = "FORWARD_PORT"


class ReferenceKind(StrEnum):
    BRANCH = "branch"
    TAG = "tag"
    SHA = "sha"


class CompletionPolicy(StrEnum):
    CODE_MERGED = "CODE_MERGED"
    VERIFIED_ARTIFACT = "VERIFIED_ARTIFACT"


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    schema_version: int
    repository_id: int
    repository_full_name: str
    work_item_id: str
    delivery_issue_number: int
    accepted_issue_body_sha256: str
    mode: DeliveryMode
    requested_ref: str
    ref_kind: ReferenceKind
    resolved_ref: str
    source_sha: str
    expected_pr_base: str
    observed_pr_base_sha: str
    expected_work_branch: str
    starting_sha: str
    correction_id: str = ""
    linked_work_item: str = ""
    release_id: str = ""
    release_branch: str = ""
    release_origin_sha: str = ""
    release_state: str = ""
    source_hotfix_pr: int | None = None
    integrated_commits: tuple[str, ...] = ()
    forward_port_method: str = ""
    completion_policy: CompletionPolicy = CompletionPolicy.CODE_MERGED

    def __post_init__(self) -> None:
        try:
            mode = DeliveryMode(self.mode)
            kind = ReferenceKind(self.ref_kind)
            policy = CompletionPolicy(self.completion_policy)
        except ValueError as exc:
            raise DeliveryContractError("Unknown delivery mode, reference kind or completion policy") from exc
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "ref_kind", kind)
        object.__setattr__(self, "completion_policy", policy)
        if self.schema_version != 1:
            raise DeliveryContractError("Unsupported delivery contract schema")
        if (type(self.repository_id) is not int or self.repository_id <= 0
                or not _REPO.fullmatch(self.repository_full_name)):
            raise DeliveryContractError("Repository ID and owner/name are mandatory")
        if (not self.work_item_id or type(self.delivery_issue_number) is not int
                or self.delivery_issue_number <= 0):
            raise DeliveryContractError("WorkItem and anchoring delivery issue are mandatory")
        for field in ("accepted_issue_body_sha256", "source_sha", "observed_pr_base_sha", "starting_sha"):
            if not _SHA.fullmatch(getattr(self, field)):
                raise DeliveryContractError(f"{field} requires a full immutable lowercase SHA")
        if not all((self.requested_ref, self.resolved_ref,
                    self.expected_pr_base, self.expected_work_branch)):
            raise DeliveryContractError("Source and target refs must be explicitly selected")
        if not _BRANCH.fullmatch(self.expected_pr_base) or not _BRANCH.fullmatch(self.expected_work_branch):
            raise DeliveryContractError("Invalid target branch")
        if kind is ReferenceKind.SHA:
            if self.requested_ref != self.source_sha or self.resolved_ref != self.source_sha:
                raise DeliveryContractError("SHA ref must use its full exact commit")
        elif kind is ReferenceKind.BRANCH:
            if (not _BRANCH.fullmatch(self.requested_ref)
                    or self.resolved_ref != f"refs/heads/{self.requested_ref}"):
                raise DeliveryContractError("Branch requires an exact qualified refs/heads/ ref")
        elif kind is ReferenceKind.TAG:
            if (not _BRANCH.fullmatch(self.requested_ref)
                    or self.resolved_ref != f"refs/tags/{self.requested_ref}"):
                raise DeliveryContractError("Tag requires an exact qualified refs/tags/ ref")
        if mode is DeliveryMode.NORMAL:
            if (self.requested_ref != "main" or kind is not ReferenceKind.BRANCH
                    or self.expected_pr_base != "main"):
                raise DeliveryContractError("NORMAL must target verified main")
        elif mode in (DeliveryMode.RELEASE, DeliveryMode.HOTFIX):
            if (not self.release_id or not self.release_branch.startswith("release/")
                    or self.expected_pr_base != self.release_branch
                    or not _SHA.fullmatch(self.release_origin_sha)
                    or self.release_state not in {"MAINTAINED", "RETIRED"}
                    or self.completion_policy is not CompletionPolicy.VERIFIED_ARTIFACT):
                raise DeliveryContractError("Release requires pinned identity, branch, origin, state and artifact policy")
            if mode is DeliveryMode.HOTFIX and (
                kind is not ReferenceKind.BRANCH or self.requested_ref != self.release_branch
                or self.release_state != "MAINTAINED" or not self.correction_id
                or not self.linked_work_item
            ):
                raise DeliveryContractError("HOTFIX requires maintained release source and linked forward-port WorkItem")
        elif mode is DeliveryMode.FORWARD_PORT:
            if (kind is not ReferenceKind.BRANCH or self.requested_ref != "main"
                    or self.expected_pr_base != "main" or not self.correction_id
                    or not self.linked_work_item or not self.source_hotfix_pr
                    or not self.integrated_commits or not self.forward_port_method):
                raise DeliveryContractError("FORWARD_PORT requires source hotfix and proven integrated delta")
        if self.source_hotfix_pr is not None and self.source_hotfix_pr <= 0:
            raise DeliveryContractError("Invalid source hotfix PR")
        if any(not _SHA.fullmatch(sha) for sha in self.integrated_commits):
            raise DeliveryContractError("Integrated commits must be full SHAs")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, content: str) -> DeliveryContext:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise DeliveryContractError("Delivery contract must be an object")
        # Reject unknown and missing fields, other than explicitly defaulted values.
        from dataclasses import fields, MISSING
        names = {field.name for field in fields(cls)}
        required = {field.name for field in fields(cls)
                    if field.default is MISSING and field.default_factory is MISSING}
        if not required <= set(payload) or not set(payload) <= names:
            raise DeliveryContractError("Delivery contract has missing or unknown fields")
        if "integrated_commits" in payload:
            if not isinstance(payload["integrated_commits"], list):
                raise DeliveryContractError("integrated_commits must be a list")
            payload["integrated_commits"] = tuple(payload["integrated_commits"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    repository_id: int
    repository_full_name: str
    resolved_ref: str
    source_commit_sha: str
    current_base_sha: str
    working_branch: str | None = None
    working_head_sha: str | None = None
    pr_base_name: str | None = None


def validate_observation(context: DeliveryContext, observed: DeliveryObservation) -> tuple[str, ...]:
    """Return fail-closed diagnostics; no mutation may proceed with any diagnostic."""
    errors: list[str] = []
    if (context.repository_id != observed.repository_id
            or context.repository_full_name != observed.repository_full_name):
        errors.append("REPOSITORY_MISMATCH")
    if observed.resolved_ref != context.resolved_ref:
        errors.append("REF_MISMATCH")
    if observed.source_commit_sha != context.source_sha:
        errors.append("SOURCE_MOVED_OR_MISSING")
    if observed.pr_base_name != context.expected_pr_base:
        errors.append("BASE_NAME_MISMATCH")
    if observed.current_base_sha != context.observed_pr_base_sha:
        errors.append("BASE_TIP_STALE")
    if observed.working_branch is not None and observed.working_branch != context.expected_work_branch:
        errors.append("WORK_BRANCH_MISMATCH")
    if observed.pr_base_name is not None and observed.pr_base_name != context.expected_pr_base:
        errors.append("PR_RETARGETED")
    # The *working* head may advance with normal commits. Starting SHA stays immutable.
    if observed.working_branch and not observed.working_head_sha:
        errors.append("WORK_BRANCH_MISSING")
    return tuple(errors)


def validate_pair(hotfix: DeliveryContext, forward_port: DeliveryContext) -> None:
    if (hotfix.mode is not DeliveryMode.HOTFIX
            or forward_port.mode is not DeliveryMode.FORWARD_PORT
            or hotfix.repository_id != forward_port.repository_id
            or hotfix.correction_id != forward_port.correction_id
            or hotfix.work_item_id == forward_port.work_item_id
            or hotfix.linked_work_item != forward_port.work_item_id
            or forward_port.linked_work_item != hotfix.work_item_id):
        raise DeliveryContractError("Hotfix and forward-port require separate predeclared linked WorkItems")


@dataclass(frozen=True, slots=True)
class DeliveryCompletionEvidence:
    merged: bool
    green_current_head: bool
    published_tag: str | None = None
    published_source_sha: str | None = None
    image_digest: str | None = None
    validated: bool = False
    deployed: bool = False


def can_reconcile_done(context: DeliveryContext, evidence: DeliveryCompletionEvidence) -> bool:
    if not evidence.merged or not evidence.green_current_head:
        return False
    if context.completion_policy is CompletionPolicy.CODE_MERGED:
        return True
    return bool(
        evidence.published_tag and evidence.published_source_sha
        and _SHA.fullmatch(evidence.published_source_sha)
        and evidence.image_digest and evidence.image_digest.startswith("sha256:")
        and len(evidence.image_digest) == 71 and evidence.validated
    )
