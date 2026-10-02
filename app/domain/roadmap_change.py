from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from difflib import unified_diff
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any
from uuid import UUID

from app.domain.roadmap import (
    PipelineParseResult,
    WorkItem,
    WorkItemStatus,
    WorkItemType,
    parse_canonical_pipeline,
    render_canonical_pipeline,
    replace_canonical_pipeline,
)


GENERATOR_VERSION = "dc041a-generator-v1"
VALIDATION_VERSION = "dc041a-validation-v1"
_ISSUE_ROW = re.compile(r"^\|\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*\|\s*#([1-9][0-9]*)\s*\|\s*$")


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    APPLIED = "APPLIED"
    CANCELLED = "CANCELLED"


class ApplicationStatus(StrEnum):
    PREPARED = "PREPARED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    CONFLICT = "CONFLICT"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ApplicationAttemptOutcome(StrEnum):
    PREPARED = "PREPARED"
    APPLYING = "APPLYING"
    NOT_EMITTED = "NOT_EMITTED"
    WRITE_RETURNED = "WRITE_RETURNED"
    APPLIED = "APPLIED"
    CONFLICT = "CONFLICT"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class RoadmapChangeProposal:
    proposal_id: UUID
    project_id: str
    repository_full_name: str
    roadmap_issue_number: int
    source_decision_id: UUID
    status: ProposalStatus
    version: int
    current_revision: int
    creation_command_id: UUID
    created_by: str
    created_at: datetime
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancellation_command_id: UUID | None = None
    confirmed_revision: int | None = None
    confirmed_preview_digest: str | None = None
    confirmation_command_id: UUID | None = None
    confirmation_expected_proposal_version: int | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    writeback_authorization_decision_id: UUID | None = None
    applied_at: datetime | None = None

    def __post_init__(self):
        ProposalStatus(self.status)
        if self.roadmap_issue_number < 1 or self.version < 1 or self.current_revision < 1:
            raise ValueError("Invalid proposal target or version")
        if not self.project_id.strip() or not self.repository_full_name.strip() or not self.created_by.strip():
            raise ValueError("Proposal identity and attribution must not be blank")
        if self.created_at.tzinfo is None:
            raise ValueError("Proposal timestamp must be timezone-aware")
        cancellation = (self.cancelled_by, self.cancelled_at, self.cancellation_command_id)
        if self.status is ProposalStatus.CANCELLED:
            if not all(cancellation) or not self.cancelled_by.strip():
                raise ValueError("Cancelled proposal requires attribution and command identity")
            if self.cancelled_at.tzinfo is None or self.cancelled_at < self.created_at:
                raise ValueError("Invalid proposal cancellation timestamp")
        elif any(value is not None for value in cancellation):
            raise ValueError("Cancellation fields require CANCELLED status")

        confirmation = (
            self.confirmed_revision,
            self.confirmed_preview_digest,
            self.confirmation_command_id,
            self.confirmation_expected_proposal_version,
            self.confirmed_by,
            self.confirmed_at,
            self.writeback_authorization_decision_id,
        )
        if self.status in {ProposalStatus.CONFIRMED, ProposalStatus.APPLIED}:
            if not all(value is not None for value in confirmation):
                raise ValueError("Confirmed proposal requires exact revision confirmation metadata")
            if (
                self.confirmed_revision < 1
                or self.confirmed_revision > self.current_revision
                or self.confirmation_expected_proposal_version < 1
                or not self.confirmed_preview_digest.strip()
                or not self.confirmed_by.strip()
                or self.confirmed_at.tzinfo is None
            ):
                raise ValueError("Invalid proposal confirmation metadata")
        elif any(value is not None for value in confirmation):
            raise ValueError("Confirmation fields require CONFIRMED or APPLIED status")

        if self.status is ProposalStatus.APPLIED:
            if self.applied_at is None or self.applied_at.tzinfo is None:
                raise ValueError("Applied proposal requires applied_at")
        elif self.applied_at is not None:
            raise ValueError("applied_at requires APPLIED status")


@dataclass(frozen=True)
class RoadmapChangeProposalRevision:
    proposal_id: UUID
    revision: int
    base_body: str
    base_body_hash: str
    base_updated_at: str | None
    proposed_body: str
    proposed_body_hash: str
    operations_json: str
    generator_version: str
    validation_version: str
    created_by: str
    created_at: datetime
    revision_command_id: UUID

    def __post_init__(self):
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        if body_hash(self.base_body) != self.base_body_hash:
            raise ValueError("base body hash does not match body")
        if body_hash(self.proposed_body) != self.proposed_body_hash:
            raise ValueError("proposed body hash does not match body")
        if not self.created_by.strip() or self.created_at.tzinfo is None:
            raise ValueError("Revision attribution and timezone-aware timestamp required")
        canonical_operations(json.loads(self.operations_json))

    @property
    def operations(self) -> tuple[dict[str, Any], ...]:
        return canonical_operations(json.loads(self.operations_json))


@dataclass(frozen=True)
class RoadmapChangeApplication:
    application_id: UUID
    proposal_id: UUID
    revision: int
    project_id: str
    repository_full_name: str
    roadmap_issue_number: int
    base_body: str
    base_body_hash: str
    expected_body: str
    expected_body_hash: str
    application_command_id: UUID
    expected_proposal_version: int
    requested_by: str
    status: ApplicationStatus
    version: int
    created_at: datetime
    updated_at: datetime
    last_remote_body_hash: str | None = None

    def __post_init__(self):
        ApplicationStatus(self.status)
        if (
            self.revision < 1
            or self.roadmap_issue_number < 1
            or self.version < 1
            or self.expected_proposal_version < 1
        ):
            raise ValueError("Invalid roadmap application identity")
        if (
            not self.project_id.strip()
            or not self.repository_full_name.strip()
            or not self.requested_by.strip()
        ):
            raise ValueError("Roadmap application target and attribution must not be blank")
        if body_hash(self.base_body) != self.base_body_hash:
            raise ValueError("Application base body hash does not match body")
        if body_hash(self.expected_body) != self.expected_body_hash:
            raise ValueError("Application expected body hash does not match body")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Application timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Application updated_at cannot precede created_at")
        if self.last_remote_body_hash is not None and len(self.last_remote_body_hash) != 64:
            raise ValueError("Invalid last remote body hash")


@dataclass(frozen=True)
class RoadmapChangeApplicationAttempt:
    attempt_id: UUID
    application_id: UUID
    attempt_number: int
    command_id: UUID
    outcome: ApplicationAttemptOutcome
    patch_may_have_been_emitted: bool
    started_at: datetime
    expected_application_version: int | None = None
    requested_by: str | None = None
    completed_at: datetime | None = None
    detail: str | None = None

    def __post_init__(self):
        ApplicationAttemptOutcome(self.outcome)
        if self.attempt_number < 1 or self.started_at.tzinfo is None:
            raise ValueError("Invalid application attempt")
        if self.expected_application_version is not None and self.expected_application_version < 1:
            raise ValueError("Invalid expected application version")
        if self.requested_by is not None and not self.requested_by.strip():
            raise ValueError("Attempt attribution must not be blank")
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None or self.completed_at < self.started_at:
                raise ValueError("Invalid application attempt completion timestamp")


@dataclass(frozen=True)
class ProposalDiagnostic:
    code: str
    message: str
    blocking: bool = True


def body_hash(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()


def canonical_operations(operations: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(operations, (list, tuple)):
        raise ValueError("operations must be a list")
    try:
        normalized = json.loads(json.dumps(operations, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError("operations must be JSON serializable") from exc
    if not isinstance(normalized, list):
        raise ValueError("operations must be a list")
    result: list[dict[str, Any]] = []
    for operation in normalized:
        if not isinstance(operation, dict) or not isinstance(operation.get("type"), str):
            raise ValueError("each operation requires a string type")
        result.append(operation)
    return tuple(result)


def operations_json(operations: Any) -> str:
    return json.dumps(
        list(canonical_operations(operations)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_replace(body: str, expected: str, replacement: str, *, operation_type: str) -> str:
    if not expected:
        raise ValueError(f"{operation_type}.expected must not be blank")
    count = body.count(expected)
    if count != 1:
        raise ValueError(f"{operation_type} precondition expected exactly one match, found {count}")
    return body.replace(expected, replacement, 1)


def parse_issue_mappings(body: str) -> dict[str, int]:
    mappings: dict[str, int] = {}
    for raw in body.splitlines():
        match = _ISSUE_ROW.fullmatch(raw.strip())
        if match:
            mappings[match.group(1)] = int(match.group(2))
    return mappings


def _update_issue_mapping(body: str, key: str, issue_number: int) -> str:
    if issue_number < 1:
        raise ValueError("issue_number must be positive")
    lines = body.splitlines()
    heading_positions = [i for i, line in enumerate(lines) if line.strip() == "## Issues de livraison"]
    if len(heading_positions) != 1:
        raise ValueError("update_issue_mapping requires exactly one '## Issues de livraison' section")
    heading = heading_positions[0]
    section_end = next(
        (i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ") and i > heading + 1),
        len(lines),
    )
    matches = [
        i for i in range(heading + 1, section_end)
        if (match := _ISSUE_ROW.fullmatch(lines[i].strip())) and match.group(1) == key
    ]
    row = f"| {key} | #{issue_number} |"
    if len(matches) > 1:
        raise ValueError(f"issue mapping for {key} is ambiguous")
    if matches:
        lines[matches[0]] = row
        return "\n".join(lines)

    table_rows = [
        i for i in range(heading + 1, section_end)
        if _ISSUE_ROW.fullmatch(lines[i].strip())
    ]
    if not table_rows:
        raise ValueError("issue mapping table not found in 'Issues de livraison'")
    insert_at = table_rows[-1] + 1
    lines.insert(insert_at, row)
    return "\n".join(lines)


def _work_item_from_operation(operation: dict[str, Any]) -> WorkItem:
    required = ("key", "item_type", "status", "parent", "lane", "title")
    missing = [name for name in required if not str(operation.get(name, "")).strip()]
    if missing:
        raise ValueError("add_work_item missing fields: " + ", ".join(missing))
    replaces = operation.get("replaces")
    if replaces in ("", "-"):
        replaces = None
    return WorkItem(
        key=str(operation["key"]).strip(),
        type=WorkItemType(str(operation["item_type"])),
        status=WorkItemStatus(str(operation["status"])),
        parent=str(operation["parent"]).strip(),
        lane=str(operation["lane"]).strip(),
        title=str(operation["title"]).strip(),
        replaces=str(replaces).strip() if replaces is not None else None,
    )


def _apply_pipeline_operations(
    parsed: PipelineParseResult,
    operations: tuple[dict[str, Any], ...],
) -> tuple[list[WorkItem], int]:
    items = list(parsed.work_items)
    force_v2 = parsed.version == 2

    def index_for(key: str) -> int:
        matches = [i for i, item in enumerate(items) if item.key == key]
        if len(matches) != 1:
            raise ValueError(f"operation target {key!r} must identify exactly one WorkItem")
        return matches[0]

    for operation in operations:
        kind = operation["type"]
        if kind in {"update_current_state_prose", "update_slice_description", "update_issue_mapping"}:
            continue
        if kind == "add_work_item":
            item = _work_item_from_operation(operation)
            if any(existing.key == item.key for existing in items):
                raise ValueError(f"add_work_item cannot reuse existing KEY {item.key}")
            after = operation.get("after")
            if after is None:
                items.append(item)
            else:
                items.insert(index_for(str(after)) + 1, item)
            force_v2 = force_v2 or item.status is WorkItemStatus.SUPERSEDED or item.replaces is not None
        elif kind == "supersede_work_item":
            key = str(operation.get("key", ""))
            position = index_for(key)
            items[position] = replace(items[position], status=WorkItemStatus.SUPERSEDED)
            force_v2 = True
        elif kind == "set_status":
            key = str(operation.get("key", ""))
            position = index_for(key)
            status = WorkItemStatus(str(operation.get("status", "")))
            items[position] = replace(items[position], status=status)
            force_v2 = force_v2 or status is WorkItemStatus.SUPERSEDED
        elif kind == "set_replacement":
            key = str(operation.get("key", ""))
            position = index_for(key)
            target = operation.get("replaces")
            target = None if target in (None, "", "-") else str(target)
            items[position] = replace(items[position], replaces=target)
            force_v2 = force_v2 or target is not None
        elif kind == "set_order":
            key = str(operation.get("key", ""))
            position = index_for(key)
            item = items.pop(position)
            after = operation.get("after")
            if after is None:
                items.insert(0, item)
            else:
                items.insert(index_for(str(after)) + 1, item)
        else:
            raise ValueError(f"unsupported roadmap operation type: {kind}")

    minimum_version = 2 if force_v2 else 1
    return items, max(int(parsed.version or 1), minimum_version)


def generate_proposed_body(base_body: str, operations: Any) -> str:
    normalized = canonical_operations(operations)
    parsed = parse_canonical_pipeline(base_body)
    if not parsed.valid:
        raise ValueError("base roadmap canonical pipeline is invalid")

    transformed = base_body
    for operation in normalized:
        kind = operation["type"]
        if kind in {"update_current_state_prose", "update_slice_description"}:
            transformed = _strict_replace(
                transformed,
                str(operation.get("expected", "")),
                str(operation.get("replacement", "")),
                operation_type=kind,
            )
        elif kind == "update_issue_mapping":
            transformed = _update_issue_mapping(
                transformed,
                str(operation.get("key", "")).strip(),
                int(operation.get("issue_number", 0)),
            )

    transformed_parsed = parse_canonical_pipeline(transformed)
    if not transformed_parsed.valid:
        raise ValueError("targeted prose transformation invalidated the canonical pipeline")
    items, version = _apply_pipeline_operations(transformed_parsed, normalized)
    rendered = render_canonical_pipeline(items, version=version)
    result = replace_canonical_pipeline(transformed, rendered)
    if not parse_canonical_pipeline(result).valid:
        raise ValueError("generated roadmap body is invalid")
    return result


def validate_transition(base_body: str, proposed_body: str, operations: Any) -> tuple[ProposalDiagnostic, ...]:
    normalized = canonical_operations(operations)
    base = parse_canonical_pipeline(base_body)
    proposed = parse_canonical_pipeline(proposed_body)
    diagnostics: list[ProposalDiagnostic] = []

    if not base.valid:
        return (ProposalDiagnostic("BASE_PIPELINE_INVALID", "Base roadmap pipeline is invalid."),)
    if not proposed.valid:
        return tuple(
            ProposalDiagnostic(item.code, item.message)
            for item in proposed.diagnostics
        )

    base_by_key = {item.key: item for item in base.work_items}
    proposed_by_key = {item.key: item for item in proposed.work_items}
    deleted = [key for key in base_by_key if key not in proposed_by_key]
    for key in deleted:
        diagnostics.append(ProposalDiagnostic(
            "HISTORICAL_WORK_ITEM_REMOVED",
            f"Historical WorkItem {key} must remain in the roadmap.",
        ))

    for key, old in base_by_key.items():
        new = proposed_by_key.get(key)
        if new is None:
            continue
        if old.type is not new.type:
            diagnostics.append(ProposalDiagnostic(
                "WORK_ITEM_KEY_REUSED",
                f"Existing KEY {key} cannot be reused for a different WorkItem type.",
            ))
        if old.status is not WorkItemStatus.DONE and new.status is WorkItemStatus.DONE:
            diagnostics.append(ProposalDiagnostic(
                "FALSE_DONE",
                f"Roadmap proposal cannot mark {key} DONE as delivery evidence.",
            ))
        if old.status is WorkItemStatus.DONE and (
            old.parent != new.parent or old.lane != new.lane or old.title != new.title
        ):
            diagnostics.append(ProposalDiagnostic(
                "WORK_ITEM_KEY_REUSED",
                f"Delivered WorkItem {key} identity must remain historical and stable.",
            ))

    for key, item in proposed_by_key.items():
        if key not in base_by_key and item.status is WorkItemStatus.DONE:
            diagnostics.append(ProposalDiagnostic(
                "NEW_DONE_WORK_ITEM",
                f"New WorkItem {key} cannot be created as DONE.",
            ))

    explicit_status = {
        str(op.get("key"))
        for op in normalized
        if op["type"] in {"set_status", "supersede_work_item"}
    }
    explicit_status.update(
        str(op.get("key"))
        for op in normalized
        if op["type"] == "add_work_item" and op.get("status") == WorkItemStatus.READY.value
    )
    old_ready = base.active_ready_item.key if base.active_ready_item else None
    new_ready = proposed.active_ready_item.key if proposed.active_ready_item else None
    if new_ready != old_ready and new_ready is not None and new_ready not in explicit_status:
        diagnostics.append(ProposalDiagnostic(
            "HIDDEN_READY_PROMOTION",
            f"READY promotion to {new_ready} is not represented by an explicit operation.",
        ))

    if new_ready is not None:
        main = [item for item in proposed.work_items if item.lane == "MAIN"]
        ready_index = next(i for i, item in enumerate(main) if item.key == new_ready)
        for item in main[:ready_index]:
            if item.type is WorkItemType.ARCHITECTURE_GATE and item.status not in {
                WorkItemStatus.DONE, WorkItemStatus.SUPERSEDED
            }:
                diagnostics.append(ProposalDiagnostic(
                    "ARCHITECTURE_GATE_SKIPPED",
                    f"READY WorkItem {new_ready} would skip unresolved gate {item.key}.",
                ))
            elif item.status is WorkItemStatus.BLOCKED:
                diagnostics.append(ProposalDiagnostic(
                    "AMBIGUOUS_ORDER",
                    f"READY WorkItem {new_ready} has earlier BLOCKED WorkItem {item.key}.",
                ))

    mappings = parse_issue_mappings(proposed_body)
    added_keys = [key for key in proposed_by_key if key not in base_by_key]
    for key in added_keys:
        if key not in mappings:
            diagnostics.append(ProposalDiagnostic(
                "MISSING_ISSUE_MAPPING",
                f"New WorkItem {key} requires an existing GitHub issue mapping before application.",
            ))

    regenerated = generate_proposed_body(base_body, normalized)
    if regenerated != proposed_body:
        diagnostics.append(ProposalDiagnostic(
            "GENERATED_BODY_MISMATCH",
            "Stored proposed body does not match the structured operations.",
        ))
    return tuple(diagnostics)


def _diff(before: str, after: str, before_name: str, after_name: str) -> str:
    return "".join(unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=before_name,
        tofile=after_name,
    ))


def _without_pipeline(body: str) -> str:
    parsed = parse_canonical_pipeline(body)
    if not parsed.valid or parsed.version is None:
        return body
    lines = body.splitlines()
    marker = f"<!-- COCKPIT_PIPELINE_V{parsed.version} -->"
    end_marker = f"<!-- /COCKPIT_PIPELINE_V{parsed.version} -->"
    start = next(i for i, line in enumerate(lines) if line.strip() == marker)
    end = next(i for i, line in enumerate(lines) if line.strip() == end_marker)
    return "\n".join((*lines[:start], *lines[end + 1:]))


def _pipeline_text(body: str) -> str:
    parsed = parse_canonical_pipeline(body)
    if not parsed.valid or parsed.version is None:
        return ""
    lines = body.splitlines()
    marker = f"<!-- COCKPIT_PIPELINE_V{parsed.version} -->"
    end_marker = f"<!-- /COCKPIT_PIPELINE_V{parsed.version} -->"
    start = next(i for i, line in enumerate(lines) if line.strip() == marker)
    end = next(i for i, line in enumerate(lines) if line.strip() == end_marker)
    return "\n".join(lines[start:end + 1]) + "\n"


def preview_digest(
    *,
    repository_full_name: str,
    roadmap_issue_number: int,
    proposal_id: UUID,
    revision: int,
    base_body: str,
    operations: Any,
    proposed_body: str,
) -> str:
    payload = {
        "target": {
            "repository_full_name": repository_full_name,
            "roadmap_issue_number": roadmap_issue_number,
        },
        "proposal_id": str(proposal_id),
        "revision": revision,
        "base_body": base_body,
        "base_body_hash": body_hash(base_body),
        "operations": list(canonical_operations(operations)),
        "proposed_body": proposed_body,
        "proposed_body_hash": body_hash(proposed_body),
    }
    return sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def build_preview(proposal: RoadmapChangeProposal, revision: RoadmapChangeProposalRevision) -> dict[str, Any]:
    base = parse_canonical_pipeline(revision.base_body)
    proposed = parse_canonical_pipeline(revision.proposed_body)
    diagnostics = validate_transition(
        revision.base_body,
        revision.proposed_body,
        revision.operations,
    )

    base_by_key = {item.key: item for item in base.work_items}
    proposed_by_key = {item.key: item for item in proposed.work_items}
    added = [asdict(item) for key, item in proposed_by_key.items() if key not in base_by_key]
    changed = [
        {"before": asdict(base_by_key[key]), "after": asdict(item)}
        for key, item in proposed_by_key.items()
        if key in base_by_key and item != base_by_key[key]
    ]
    superseded = [
        item.key for item in proposed.work_items
        if item.status is WorkItemStatus.SUPERSEDED
        and (item.key not in base_by_key or base_by_key[item.key].status is not WorkItemStatus.SUPERSEDED)
    ]
    status_changes = [
        {"key": key, "from": base_by_key[key].status.value, "to": item.status.value}
        for key, item in proposed_by_key.items()
        if key in base_by_key and item.status is not base_by_key[key].status
    ]
    replacements = [
        {"key": item.key, "replaces": item.replaces}
        for item in proposed.work_items if item.replaces is not None
    ]
    digest = preview_digest(
        repository_full_name=proposal.repository_full_name,
        roadmap_issue_number=proposal.roadmap_issue_number,
        proposal_id=proposal.proposal_id,
        revision=revision.revision,
        base_body=revision.base_body,
        operations=revision.operations,
        proposed_body=revision.proposed_body,
    )
    return {
        "project": proposal.project_id,
        "repository": proposal.repository_full_name,
        "roadmap_issue": proposal.roadmap_issue_number,
        "proposal_id": str(proposal.proposal_id),
        "revision": revision.revision,
        "preview_digest": digest,
        "base_body": revision.base_body,
        "proposed_body": revision.proposed_body,
        "full_diff": _diff(revision.base_body, revision.proposed_body, "base", "proposed"),
        "human_text_changes": _diff(
            _without_pipeline(revision.base_body),
            _without_pipeline(revision.proposed_body),
            "base-human",
            "proposed-human",
        ),
        "canonical_pipeline_changes": _diff(
            _pipeline_text(revision.base_body),
            _pipeline_text(revision.proposed_body),
            "base-pipeline",
            "proposed-pipeline",
        ),
        "added_items": added,
        "changed_items": changed,
        "superseded_items": superseded,
        "ordering": [item.key for item in proposed.work_items],
        "status_changes": status_changes,
        "old_ready": base.active_ready_item.key if base.active_ready_item else None,
        "new_ready": proposed.active_ready_item.key if proposed.active_ready_item else None,
        "replaces_relationships": replacements,
        "issue_mappings": parse_issue_mappings(revision.proposed_body),
        "blocking_diagnostics": [asdict(item) for item in diagnostics if item.blocking],
        "allowed_actions": ["CREATE_REVISION", "CANCEL"] if proposal.status is ProposalStatus.DRAFT else [],
        "generator_version": revision.generator_version,
        "validation_version": revision.validation_version,
    }
