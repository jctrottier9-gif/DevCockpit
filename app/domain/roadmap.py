from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


V1_START_MARKER = "<!-- COCKPIT_PIPELINE_V1 -->"
V1_END_MARKER = "<!-- /COCKPIT_PIPELINE_V1 -->"
V2_START_MARKER = "<!-- COCKPIT_PIPELINE_V2 -->"
V2_END_MARKER = "<!-- /COCKPIT_PIPELINE_V2 -->"
START_MARKER = V1_START_MARKER
END_MARKER = V1_END_MARKER
V1_COLUMNS = ("KEY", "TYPE", "STATUS", "PARENT", "LANE", "TITLE")
V2_COLUMNS = ("KEY", "TYPE", "STATUS", "PARENT", "LANE", "TITLE", "REPLACES")
EXPECTED_COLUMNS = V1_COLUMNS
_PARENT_PATTERN = re.compile(r"^#[1-9][0-9]*$")
_VERSION_MARKER_PATTERN = re.compile(r"^<!--\s*/?COCKPIT_PIPELINE_V([0-9]+)\s*-->$")


class WorkItemType(StrEnum):
    WORK = "WORK"
    ARCHITECTURE_GATE = "ARCHITECTURE_GATE"


class WorkItemStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    type: WorkItemType
    status: WorkItemStatus
    parent: str
    lane: str
    title: str
    replaces: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineDiagnostic:
    code: str
    message: str
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class PipelineParseResult:
    valid: bool
    work_items: tuple[WorkItem, ...]
    diagnostics: tuple[PipelineDiagnostic, ...]
    active_ready_item: WorkItem | None
    version: int | None = None


def _diagnostic(code: str, message: str, line_number: int | None = None) -> PipelineDiagnostic:
    return PipelineDiagnostic(code=code, message=message, line_number=line_number)


def _invalid(*diagnostics: PipelineDiagnostic, version: int | None = None) -> PipelineParseResult:
    return PipelineParseResult(False, (), tuple(diagnostics), None, version)


def _split_row(raw_line: str) -> list[str]:
    return [cell.strip() for cell in raw_line.split("|")]


def _select_version(lines: list[str]) -> tuple[int | None, list[PipelineDiagnostic]]:
    positions = {1: {"start": [], "end": []}, 2: {"start": [], "end": []}}
    unknown: list[tuple[int, int]] = []
    for index, raw in enumerate(lines):
        line = raw.strip()
        match = _VERSION_MARKER_PATTERN.fullmatch(line)
        if not match:
            continue
        version = int(match.group(1))
        is_end = line.startswith("<!-- /")
        if version in positions:
            positions[version]["end" if is_end else "start"].append(index)
        else:
            unknown.append((index + 1, version))

    if unknown:
        line_number, version = unknown[0]
        return None, [_diagnostic(
            "UNKNOWN_PIPELINE_VERSION",
            f"unsupported canonical pipeline version: V{version}",
            line_number,
        )]

    present = [version for version, markers in positions.items() if markers["start"] or markers["end"]]
    if len(present) > 1:
        return None, [_diagnostic(
            "MULTIPLE_PIPELINE_VERSIONS",
            "roadmap must not contain V1 and V2 canonical pipeline blocks simultaneously",
        )]
    if not present:
        return None, [
            _diagnostic("MISSING_START_MARKER", "canonical pipeline start marker is missing"),
            _diagnostic("MISSING_END_MARKER", "canonical pipeline end marker is missing"),
        ]

    version = present[0]
    markers = positions[version]
    diagnostics: list[PipelineDiagnostic] = []
    if not markers["start"]:
        diagnostics.append(_diagnostic("MISSING_START_MARKER", "canonical pipeline start marker is missing"))
    if not markers["end"]:
        diagnostics.append(_diagnostic("MISSING_END_MARKER", "canonical pipeline end marker is missing"))
    if len(markers["start"]) > 1 or len(markers["end"]) > 1:
        diagnostics.append(_diagnostic("MULTIPLE_CANONICAL_BLOCK", "canonical pipeline block must be unique"))
    return version, diagnostics


def _marker_positions(lines: list[str], version: int) -> tuple[int, int]:
    start_marker = V1_START_MARKER if version == 1 else V2_START_MARKER
    end_marker = V1_END_MARKER if version == 1 else V2_END_MARKER
    start = next(index for index, line in enumerate(lines) if line.strip() == start_marker)
    end = next(index for index, line in enumerate(lines) if line.strip() == end_marker)
    return start, end


def _replacement_diagnostics(work_items: list[WorkItem]) -> list[PipelineDiagnostic]:
    diagnostics: list[PipelineDiagnostic] = []
    by_key = {item.key: item for item in work_items}
    replaced_by: dict[str, list[str]] = {}
    for item in work_items:
        target_key = item.replaces
        if target_key is None:
            continue
        if target_key == item.key:
            diagnostics.append(_diagnostic("SELF_REPLACEMENT", f"{item.key} cannot replace itself"))
            continue
        target = by_key.get(target_key)
        if target is None:
            diagnostics.append(_diagnostic(
                "REPLACEMENT_TARGET_MISSING",
                f"{item.key} references missing REPLACES target {target_key}",
            ))
            continue
        if target.status is not WorkItemStatus.SUPERSEDED:
            diagnostics.append(_diagnostic(
                "REPLACEMENT_TARGET_NOT_SUPERSEDED",
                f"{item.key} can replace only a SUPERSEDED WorkItem; {target_key} is {target.status.value}",
            ))
        replaced_by.setdefault(target_key, []).append(item.key)

    for item in work_items:
        if item.status is WorkItemStatus.SUPERSEDED and not replaced_by.get(item.key):
            diagnostics.append(_diagnostic(
                "SUPERSEDED_WITHOUT_REPLACEMENT",
                f"{item.key} is SUPERSEDED but no WorkItem replaces it",
            ))

    graph = {item.key: item.replaces for item in work_items if item.replaces and item.replaces in by_key}
    cycle_nodes: set[str] = set()
    for start in graph:
        path: list[str] = []
        current: str | None = start
        while current is not None and current in graph:
            if current in path:
                cycle_nodes.update(path[path.index(current):])
                break
            path.append(current)
            current = graph.get(current)
    if cycle_nodes:
        diagnostics.append(_diagnostic(
            "REPLACEMENT_CYCLE",
            "replacement cycle detected: " + ", ".join(sorted(cycle_nodes)),
        ))
    return diagnostics


def parse_canonical_pipeline(body: str) -> PipelineParseResult:
    """Parse exactly one explicit V1 or V2 canonical block and fail closed."""

    lines = body.splitlines()
    version, diagnostics = _select_version(lines)
    if diagnostics:
        return _invalid(*diagnostics, version=version)
    assert version in (1, 2)

    start, end = _marker_positions(lines, version)
    if end <= start:
        return _invalid(
            _diagnostic("INVALID_MARKER_ORDER", "canonical pipeline end marker must follow the start marker"),
            version=version,
        )

    block = [(index + 1, lines[index].strip()) for index in range(start + 1, end) if lines[index].strip()]
    if not block:
        return _invalid(_diagnostic("EMPTY_PIPELINE", "canonical pipeline block is empty"), version=version)

    expected_columns = V1_COLUMNS if version == 1 else V2_COLUMNS
    header_line_number, header = block[0]
    header_cells = _split_row(header)
    if header_cells != list(expected_columns):
        header_diagnostics: list[PipelineDiagnostic] = []
        expected = set(expected_columns)
        actual = set(header_cells)
        if len(header_cells) < len(expected_columns) or expected - actual:
            header_diagnostics.append(_diagnostic(
                "MISSING_COLUMN", "pipeline header is missing one or more required columns", header_line_number
            ))
        if len(header_cells) > len(expected_columns) or actual - expected:
            header_diagnostics.append(_diagnostic(
                "EXTRA_COLUMN", "pipeline header contains one or more unknown columns", header_line_number
            ))
        if not header_diagnostics:
            header_diagnostics.append(_diagnostic(
                "INVALID_HEADER_ORDER", "pipeline columns must use the canonical order", header_line_number
            ))
        return _invalid(*header_diagnostics, version=version)

    if len(block) == 1:
        return _invalid(
            _diagnostic("EMPTY_PIPELINE", "canonical pipeline contains a header but no WorkItems", header_line_number),
            version=version,
        )

    row_diagnostics: list[PipelineDiagnostic] = []
    work_items: list[WorkItem] = []
    seen_keys: set[str] = set()
    v1_statuses = {WorkItemStatus.READY, WorkItemStatus.BLOCKED, WorkItemStatus.DONE}

    for line_number, row in block[1:]:
        cells = _split_row(row)
        if len(cells) != len(expected_columns):
            row_diagnostics.append(_diagnostic(
                "MALFORMED_ROW",
                f"pipeline row must contain exactly {len(expected_columns)} columns",
                line_number,
            ))
            continue

        if version == 1:
            key, raw_type, raw_status, parent, lane, title = cells
            raw_replaces = "-"
        else:
            key, raw_type, raw_status, parent, lane, title, raw_replaces = cells

        row_valid = True
        if not key:
            row_diagnostics.append(_diagnostic("EMPTY_KEY", "WorkItem KEY must not be empty", line_number))
            row_valid = False
        elif key in seen_keys:
            row_diagnostics.append(_diagnostic("DUPLICATE_KEY", f"duplicate WorkItem KEY: {key}", line_number))
            row_valid = False
        else:
            seen_keys.add(key)

        try:
            item_type = WorkItemType(raw_type)
        except ValueError:
            row_diagnostics.append(_diagnostic("UNKNOWN_TYPE", f"unknown WorkItem TYPE: {raw_type}", line_number))
            row_valid = False
            item_type = None

        try:
            status = WorkItemStatus(raw_status)
            if version == 1 and status not in v1_statuses:
                raise ValueError
        except ValueError:
            row_diagnostics.append(_diagnostic("UNKNOWN_STATUS", f"unknown WorkItem STATUS: {raw_status}", line_number))
            row_valid = False
            status = None

        if not _PARENT_PATTERN.fullmatch(parent):
            row_diagnostics.append(_diagnostic(
                "INVALID_PARENT", "PARENT must be a positive GitHub issue reference such as #502", line_number
            ))
            row_valid = False
        if not lane:
            row_diagnostics.append(_diagnostic("EMPTY_LANE", "LANE must not be empty", line_number))
            row_valid = False
        if not title:
            row_diagnostics.append(_diagnostic("EMPTY_TITLE", "TITLE must not be empty", line_number))
            row_valid = False

        replaces: str | None = None
        if version == 2:
            if not raw_replaces:
                row_diagnostics.append(_diagnostic(
                    "INVALID_REPLACES", "REPLACES must be '-' or an existing WorkItem KEY", line_number
                ))
                row_valid = False
            elif raw_replaces != "-":
                replaces = raw_replaces

        if row_valid and item_type is not None and status is not None:
            work_items.append(WorkItem(key, item_type, status, parent, lane, title, replaces))

    main_ready = [
        item for item in work_items
        if item.lane == "MAIN" and item.status is WorkItemStatus.READY
    ]
    if len(main_ready) > 1:
        row_diagnostics.append(_diagnostic(
            "MULTIPLE_MAIN_READY", "MAIN is sequential and may contain at most one READY WorkItem"
        ))

    if version == 2 and not row_diagnostics:
        row_diagnostics.extend(_replacement_diagnostics(work_items))

    valid = not row_diagnostics
    active_ready_item = main_ready[0] if valid and len(main_ready) == 1 else None
    return PipelineParseResult(valid, tuple(work_items), tuple(row_diagnostics), active_ready_item, version)


def render_canonical_pipeline(work_items: tuple[WorkItem, ...] | list[WorkItem], *, version: int) -> str:
    if version == 1:
        if any(item.status is WorkItemStatus.SUPERSEDED or item.replaces for item in work_items):
            raise ValueError("V1 cannot represent SUPERSEDED or REPLACES")
        rows = [
            " | ".join((item.key, item.type.value, item.status.value, item.parent, item.lane, item.title))
            for item in work_items
        ]
        return "\n".join((V1_START_MARKER, " | ".join(V1_COLUMNS), *rows, V1_END_MARKER))
    if version != 2:
        raise ValueError(f"Unsupported canonical pipeline version: {version}")
    rows = [
        " | ".join((
            item.key, item.type.value, item.status.value, item.parent,
            item.lane, item.title, item.replaces or "-"
        ))
        for item in work_items
    ]
    return "\n".join((V2_START_MARKER, " | ".join(V2_COLUMNS), *rows, V2_END_MARKER))


def replace_canonical_pipeline(body: str, rendered_block: str) -> str:
    parsed = parse_canonical_pipeline(body)
    if not parsed.valid or parsed.version not in (1, 2):
        raise ValueError("Cannot replace an invalid canonical pipeline")
    lines = body.splitlines()
    start, end = _marker_positions(lines, parsed.version)
    replacement = rendered_block.splitlines()
    return "\n".join((*lines[:start], *replacement, *lines[end + 1:]))
