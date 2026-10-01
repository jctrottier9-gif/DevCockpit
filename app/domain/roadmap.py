from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


START_MARKER = "<!-- COCKPIT_PIPELINE_V1 -->"
END_MARKER = "<!-- /COCKPIT_PIPELINE_V1 -->"
EXPECTED_COLUMNS = ("KEY", "TYPE", "STATUS", "PARENT", "LANE", "TITLE")
_PARENT_PATTERN = re.compile(r"^#[1-9][0-9]*$")


class WorkItemType(StrEnum):
    WORK = "WORK"
    ARCHITECTURE_GATE = "ARCHITECTURE_GATE"


class WorkItemStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    type: WorkItemType
    status: WorkItemStatus
    parent: str
    lane: str
    title: str


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


def _diagnostic(code: str, message: str, line_number: int | None = None) -> PipelineDiagnostic:
    return PipelineDiagnostic(code=code, message=message, line_number=line_number)


def _split_row(raw_line: str) -> list[str]:
    return [cell.strip() for cell in raw_line.split("|")]


def parse_canonical_pipeline(body: str) -> PipelineParseResult:
    """Parse exactly one canonical roadmap block and fail closed on ambiguity."""

    lines = body.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == START_MARKER]
    ends = [index for index, line in enumerate(lines) if line.strip() == END_MARKER]
    diagnostics: list[PipelineDiagnostic] = []

    if not starts:
        diagnostics.append(_diagnostic("MISSING_START_MARKER", "canonical pipeline start marker is missing"))
    if not ends:
        diagnostics.append(_diagnostic("MISSING_END_MARKER", "canonical pipeline end marker is missing"))
    if len(starts) > 1 or len(ends) > 1:
        diagnostics.append(_diagnostic("MULTIPLE_CANONICAL_BLOCK", "canonical pipeline block must be unique"))
    if diagnostics:
        return PipelineParseResult(False, (), tuple(diagnostics), None)

    start = starts[0]
    end = ends[0]
    if end <= start:
        return PipelineParseResult(
            False,
            (),
            (_diagnostic("INVALID_MARKER_ORDER", "canonical pipeline end marker must follow the start marker"),),
            None,
        )

    block = [(index + 1, lines[index].strip()) for index in range(start + 1, end) if lines[index].strip()]
    if not block:
        return PipelineParseResult(
            False,
            (),
            (_diagnostic("EMPTY_PIPELINE", "canonical pipeline block is empty"),),
            None,
        )

    header_line_number, header = block[0]
    header_cells = _split_row(header)
    if header_cells != list(EXPECTED_COLUMNS):
        expected = set(EXPECTED_COLUMNS)
        actual = set(header_cells)
        if len(header_cells) < len(EXPECTED_COLUMNS) or expected - actual:
            diagnostics.append(
                _diagnostic("MISSING_COLUMN", "pipeline header is missing one or more required columns", header_line_number)
            )
        if len(header_cells) > len(EXPECTED_COLUMNS) or actual - expected:
            diagnostics.append(
                _diagnostic("EXTRA_COLUMN", "pipeline header contains one or more unknown columns", header_line_number)
            )
        if not diagnostics:
            diagnostics.append(
                _diagnostic("INVALID_HEADER_ORDER", "pipeline columns must use the canonical order", header_line_number)
            )
        return PipelineParseResult(False, (), tuple(diagnostics), None)

    if len(block) == 1:
        return PipelineParseResult(
            False,
            (),
            (_diagnostic("EMPTY_PIPELINE", "canonical pipeline contains a header but no WorkItems", header_line_number),),
            None,
        )

    work_items: list[WorkItem] = []
    seen_keys: set[str] = set()

    for line_number, row in block[1:]:
        cells = _split_row(row)
        if len(cells) != len(EXPECTED_COLUMNS):
            diagnostics.append(
                _diagnostic("MALFORMED_ROW", "pipeline row must contain exactly six columns", line_number)
            )
            continue

        key, raw_type, raw_status, parent, lane, title = cells
        row_valid = True

        if not key:
            diagnostics.append(_diagnostic("EMPTY_KEY", "WorkItem KEY must not be empty", line_number))
            row_valid = False
        elif key in seen_keys:
            diagnostics.append(_diagnostic("DUPLICATE_KEY", f"duplicate WorkItem KEY: {key}", line_number))
            row_valid = False
        else:
            seen_keys.add(key)

        try:
            item_type = WorkItemType(raw_type)
        except ValueError:
            diagnostics.append(_diagnostic("UNKNOWN_TYPE", f"unknown WorkItem TYPE: {raw_type}", line_number))
            row_valid = False
            item_type = None

        try:
            status = WorkItemStatus(raw_status)
        except ValueError:
            diagnostics.append(_diagnostic("UNKNOWN_STATUS", f"unknown WorkItem STATUS: {raw_status}", line_number))
            row_valid = False
            status = None

        if not _PARENT_PATTERN.fullmatch(parent):
            diagnostics.append(
                _diagnostic("INVALID_PARENT", "PARENT must be a positive GitHub issue reference such as #502", line_number)
            )
            row_valid = False
        if not lane:
            diagnostics.append(_diagnostic("EMPTY_LANE", "LANE must not be empty", line_number))
            row_valid = False
        if not title:
            diagnostics.append(_diagnostic("EMPTY_TITLE", "TITLE must not be empty", line_number))
            row_valid = False

        if row_valid and item_type is not None and status is not None:
            work_items.append(
                WorkItem(
                    key=key,
                    type=item_type,
                    status=status,
                    parent=parent,
                    lane=lane,
                    title=title,
                )
            )

    main_ready = [
        item for item in work_items if item.lane == "MAIN" and item.status is WorkItemStatus.READY
    ]
    if len(main_ready) > 1:
        diagnostics.append(
            _diagnostic("MULTIPLE_MAIN_READY", "MAIN is sequential and may contain at most one READY WorkItem")
        )

    valid = not diagnostics
    active_ready_item = main_ready[0] if valid and len(main_ready) == 1 else None
    return PipelineParseResult(valid, tuple(work_items), tuple(diagnostics), active_ready_item)
