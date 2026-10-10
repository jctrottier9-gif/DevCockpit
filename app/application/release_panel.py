"""Read-only, fail-closed release/hotfix/forward-port cockpit projection.

The accepted DeliveryContext, canonical V3 roadmap and observed GitHub evidence
remain separate authorities. This module never creates branches, PRs or tags.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from app.application.executions import ExecutionSourceError
from app.application.roadmaps import read_project_roadmap
from app.domain.delivery_context import DeliveryMode, validate_pair, DeliveryContractError
from app.domain.execution import derive_execution_projection
from app.domain.roadmap import WorkItemStatus
from app.infrastructure.github_release_workflow import ReleaseWorkflowError


def read_project_release_panel(project, *, roadmap_reader, evidence_reader,
                               artifact_reader, uow_factory, now=None) -> dict:
    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    contexts = tuple(
        context for context in project.delivery_contexts
        if context.mode in {DeliveryMode.RELEASE, DeliveryMode.HOTFIX, DeliveryMode.FORWARD_PORT}
    )
    observed_at = (now or datetime.now(timezone.utc)).isoformat()
    revision = sha256(roadmap.issue.body.encode("utf-8")).hexdigest()
    source = {"status": "available" if roadmap.pipeline.valid else "unavailable",
              "revision": revision, "updated_at": roadmap.issue.updated_at}
    if not roadmap.pipeline.valid:
        return {
            "project": {"project_id": project.project_id,
                        "repository_full_name": project.repository_full_name},
            "observed_at": observed_at,
            "source": {**source, "code": "INVALID_CANONICAL_ROADMAP"},
            "items": [],
            "diagnostics": [item.code for item in roadmap.pipeline.diagnostics],
        }

    work_items = {item.key: item for item in roadmap.pipeline.work_items}
    with uow_factory() as uow:
        accepted = {
            context.work_item_id:
            uow.delivery_contexts.get(context.repository_id, context.work_item_id) == context
            for context in contexts
        }

    rows: list[dict] = []
    for context in contexts:
        item = work_items.get(context.work_item_id)
        diagnostics: list[str] = []
        if not accepted[context.work_item_id]:
            diagnostics.append("DELIVERY_NOT_ACCEPTED")
        if item is None:
            diagnostics.append("WORK_ITEM_NOT_IN_ROADMAP")
        if context.mode is DeliveryMode.HOTFIX or context.mode is DeliveryMode.FORWARD_PORT:
            counterpart = project.delivery_context_for(context.linked_work_item)
            try:
                if counterpart is None:
                    raise DeliveryContractError("Linked delivery is missing")
                hotfix, forward = (
                    (context, counterpart) if context.mode is DeliveryMode.HOTFIX
                    else (counterpart, context)
                )
                validate_pair(hotfix, forward)
                if not accepted.get(counterpart.work_item_id, False):
                    diagnostics.append("LINKED_DELIVERY_NOT_ACCEPTED")
            except DeliveryContractError:
                diagnostics.append("INVALID_DELIVERY_PAIR")

        row = {
            "work_item_id": context.work_item_id,
            "issue_url": (
                f"https://github.com/{project.repository_full_name}/issues/"
                f"{context.delivery_issue_number}"
            ),
            "mode": context.mode.value,
            "roadmap_status": item.status.value if item else "UNKNOWN",
            "correction_id": context.correction_id or None,
            "linked_work_item": context.linked_work_item or None,
            "release_id": context.release_id or None,
            "release_branch": context.release_branch or None,
            "release_origin_sha": context.release_origin_sha or None,
            "release_state": context.release_state or None,
            "source_kind": context.ref_kind.value,
            "requested_ref": context.requested_ref,
            "resolved_ref": context.resolved_ref,
            "source_sha": context.source_sha,
            "target_branch": context.expected_pr_base,
            "accepted_base_sha": context.observed_pr_base_sha,
            "work_branch": context.expected_work_branch,
            "starting_sha": context.starting_sha,
            "fingerprint": context.fingerprint(),
            "integrated_commits": list(context.integrated_commits),
            "source_hotfix_pr": context.source_hotfix_pr,
            "forward_port_method": context.forward_port_method or None,
            "completion_policy": context.completion_policy.value,
            "delivery_state": "BLOCKED" if diagnostics else "NOT_OBSERVED",
            "ci_state": "NOT_OBSERVED",
            "pr": None,
            "validations": [],
            "artifact": {
                "status": "NOT_APPLICABLE" if context.mode is DeliveryMode.FORWARD_PORT
                else "NOT_VERIFIED",
                "tag": None, "source_sha": None, "image": None, "digest": None,
                "validation_check": None,
            },
            "deployment": "NOT_VERIFIED",
            "sql_compatibility": "NOT_ATTESTED",
            "diagnostics": diagnostics,
        }
        # A configured snapshot is never enough to claim a verified GitHub delivery.
        if diagnostics:
            rows.append(row)
            continue
        if item.status is WorkItemStatus.BLOCKED:
            row["delivery_state"] = "BLOCKED"
            row["diagnostics"].append("CANONICAL_DEPENDENCY_BLOCKED")
            rows.append(row)
            continue
        try:
            evidence = evidence_reader.read(project, item)
            execution = derive_execution_projection(item, evidence)
            pr = execution.pull_request
            if pr is not None:
                # An adapter may be replaced in tests or integrations: maintain
                # the same exact accepted PR identity at the projection boundary.
                required_lines = {
                    "Work-Item: " + context.work_item_id,
                    "Delivery-Context-SHA256: " + context.fingerprint(),
                }
                if (pr.branch != context.expected_work_branch
                        or pr.base_branch != context.expected_pr_base
                        or not required_lines.issubset(set(pr.body.splitlines()))):
                    row["delivery_state"] = "BLOCKED"
                    row["diagnostics"].append("PR_TARGET_OR_CONTEXT_MISMATCH")
                    rows.append(row)
                    continue
                row["pr"] = {
                    "number": pr.number, "url": pr.url, "head_sha": pr.head_sha,
                    "base_branch": pr.base_branch, "merge_commit_sha": pr.merge_commit_sha,
                    "merged": pr.merged, "mergeable_state": pr.mergeable_state,
                }
            row["delivery_state"] = (
                "MERGED" if pr is not None and pr.merged
                else execution.state.value
            )
            if execution.ci is not None:
                row["ci_state"] = execution.ci.state.value
                row["validations"] = [
                    {"run_id": run.run_id, "name": run.name, "status": run.status,
                     "conclusion": run.conclusion, "head_sha": run.head_sha, "url": run.url}
                    for run in execution.ci.runs
                ]
            row["diagnostics"].extend(d.code for d in execution.diagnostics)
            if (item.status is WorkItemStatus.DONE and pr is None
                    and context.mode is not DeliveryMode.RELEASE):
                row["delivery_state"] = "NOT_OBSERVED"
                row["diagnostics"].append("DELIVERY_PR_NOT_OBSERVED")
            if context.mode is DeliveryMode.HOTFIX and pr is not None and pr.merged:
                if not pr.merge_commit_sha:
                    row["diagnostics"].append("INTEGRATED_SHA_NOT_OBSERVED")
                else:
                    artifact = artifact_reader.find_published_artifact(
                        project, context, integrated_sha=pr.merge_commit_sha
                    )
                    if artifact is not None and evidence.artifact_verified:
                        row["artifact"] = {
                            "status": "GITHUB_VERIFIED", "tag": artifact.tag,
                            "source_sha": artifact.source_sha, "image": artifact.image,
                            "digest": artifact.digest,
                            "validation_check": artifact.validation_check,
                        }
                    else:
                        row["artifact"]["status"] = "PENDING"
                        row["diagnostics"].append("PUBLICATION_OR_VALIDATION_PENDING")
            if context.mode is DeliveryMode.HOTFIX and pr is not None and not pr.merged:
                row["artifact"]["status"] = "PENDING"
        except (ExecutionSourceError, ReleaseWorkflowError, ValueError) as exc:
            row["delivery_state"] = "BLOCKED"
            row["ci_state"] = "UNKNOWN"
            row["pr"] = None
            row["validations"] = []
            row["artifact"]["status"] = "NOT_VERIFIED"
            row["diagnostics"].append(getattr(exc, "code", "GITHUB_EVIDENCE_UNAVAILABLE"))
        rows.append(row)

    return {
        "project": {"project_id": project.project_id,
                    "repository_full_name": project.repository_full_name},
        "observed_at": observed_at, "source": source,
        "items": rows, "diagnostics": [],
    }
