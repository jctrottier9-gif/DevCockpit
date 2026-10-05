from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Protocol

from app.application.roadmaps import RoadmapIssueReader, read_project_roadmap
from app.domain.project import Project
from app.domain.roadmap import WorkItemStatus
from app.domain.scheduler import derive_scheduler_projection


_ISSUE_MAPPING_HEADING = "## Issues de livraison"
_ISSUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ISSUE_NUMBER = re.compile(r"^#?([1-9][0-9]*)$")
_ISSUE_LINK = re.compile(r"^\[#?([1-9][0-9]*)\]\([^\s)]+\)$")


@dataclass(frozen=True, slots=True)
class RoadmapExplorerDiagnostic:
    code: str
    message: str
    work_item_key: str | None = None
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class RoadmapIssueReference:
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class RoadmapExplorerItem:
    key: str
    title: str
    type: str
    status: str
    lane: str
    parent: str
    replaces: str | None
    depends_on: tuple[str, ...]
    unsatisfied_dependencies: tuple[str, ...]
    scheduler_state: str | None
    scheduler_reason: str | None
    expected_role: str | None
    next_action: str | None
    executable: bool
    work_issue: RoadmapIssueReference | None
    parent_issue: RoadmapIssueReference | None


@dataclass(frozen=True, slots=True)
class RoadmapExplorerProjection:
    project: Project
    observed_at: datetime
    roadmap_issue: RoadmapIssueReference
    roadmap_updated_at: str | None
    revision: str
    pipeline_valid: bool
    pipeline_version: int | None
    pipeline_diagnostics: tuple[RoadmapExplorerDiagnostic, ...]
    scheduler_valid: bool
    scheduler_diagnostics: tuple[RoadmapExplorerDiagnostic, ...]
    issue_mapping_diagnostics: tuple[RoadmapExplorerDiagnostic, ...]
    items: tuple[RoadmapExplorerItem, ...]
    now: str | None
    parallel: tuple[str, ...]
    next: str | None
    later: tuple[str, ...]
    history: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoadmapExplorerIssueDetail:
    repository_full_name: str
    number: int
    title: str
    body: str
    state: str
    url: str
    updated_at: str | None


class RoadmapExplorerIssueReader(Protocol):
    def read(self, repository_full_name: str, issue_number: int) -> RoadmapExplorerIssueDetail: ...


class RoadmapExplorerIssueError(RuntimeError):
    code = "GITHUB_ISSUE_UNAVAILABLE"


class RoadmapExplorerIssueAuthorizationError(RoadmapExplorerIssueError):
    code = "GITHUB_AUTHORIZATION_FAILED"


class RoadmapExplorerIssueNotFoundError(RoadmapExplorerIssueError):
    code = "GITHUB_ISSUE_NOT_FOUND"


class RoadmapExplorerIssuePayloadError(RoadmapExplorerIssueError):
    code = "GITHUB_PAYLOAD_INVALID"


class RoadmapExplorerIssueNotReferencedError(RuntimeError):
    code = "ISSUE_NOT_REFERENCED"


def _issue_url(repository_full_name: str, issue_number: int) -> str:
    return f"https://github.com/{repository_full_name}/issues/{issue_number}"


def _issue_reference(repository_full_name: str, issue_number: int) -> RoadmapIssueReference:
    return RoadmapIssueReference(
        number=issue_number,
        url=_issue_url(repository_full_name, issue_number),
    )


def _mapping_section(lines: list[str]) -> tuple[list[str], list[RoadmapExplorerDiagnostic]]:
    headings = [index for index, line in enumerate(lines) if line.strip() == _ISSUE_MAPPING_HEADING]
    if not headings:
        return [], [RoadmapExplorerDiagnostic(
            "ISSUE_MAPPING_SECTION_MISSING",
            "Roadmap does not contain the expected 'Issues de livraison' mapping section.",
        )]
    if len(headings) > 1:
        return [], [RoadmapExplorerDiagnostic(
            "ISSUE_MAPPING_SECTION_AMBIGUOUS",
            "Roadmap contains multiple 'Issues de livraison' mapping sections.",
        )]

    start = headings[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if re.match(r"^##\s+", lines[index].strip())
        ),
        len(lines),
    )
    return lines[start:end], []


def _parse_issue_number(cell: str) -> int | None:
    normalized = cell.strip()
    match = _ISSUE_NUMBER.fullmatch(normalized) or _ISSUE_LINK.fullmatch(normalized)
    return int(match.group(1)) if match else None


def parse_documented_issue_mappings(
    body: str,
) -> tuple[dict[str, int], tuple[RoadmapExplorerDiagnostic, ...]]:
    section, diagnostics = _mapping_section(body.splitlines())
    if diagnostics:
        return {}, tuple(diagnostics)

    candidates: dict[str, list[int]] = {}
    for raw in section:
        line = raw.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2 or not _ISSUE_KEY.fullmatch(cells[0]):
            continue
        issue_number = _parse_issue_number(cells[1])
        if issue_number is None:
            continue
        candidates.setdefault(cells[0], []).append(issue_number)

    mappings: dict[str, int] = {}
    result_diagnostics: list[RoadmapExplorerDiagnostic] = []
    for key, numbers in candidates.items():
        unique_numbers = set(numbers)
        if len(numbers) > 1 and len(unique_numbers) == 1:
            result_diagnostics.append(RoadmapExplorerDiagnostic(
                "DUPLICATE_ISSUE_MAPPING",
                f"{key} appears more than once in the issue mapping section.",
                key,
            ))
            continue
        if len(unique_numbers) > 1:
            result_diagnostics.append(RoadmapExplorerDiagnostic(
                "CONFLICTING_ISSUE_MAPPING",
                f"{key} maps to multiple GitHub issues: " + ", ".join(
                    f"#{number}" for number in sorted(unique_numbers)
                ),
                key,
            ))
            continue
        mappings[key] = numbers[0]

    return mappings, tuple(result_diagnostics)


def _derive_horizons(pipeline) -> tuple[
    str | None,
    tuple[str, ...],
    str | None,
    tuple[str, ...],
    tuple[str, ...],
]:
    if not pipeline.valid:
        return None, (), None, (), ()

    unfinished = {WorkItemStatus.READY, WorkItemStatus.BLOCKED}
    main = [
        item.key
        for item in pipeline.work_items
        if item.lane == "MAIN" and item.status in unfinished
    ]
    parallel = tuple(
        item.key
        for item in pipeline.work_items
        if item.lane != "MAIN" and item.status in unfinished
    )
    history = tuple(
        item.key
        for item in pipeline.work_items
        if item.status in {WorkItemStatus.DONE, WorkItemStatus.SUPERSEDED}
    )
    return (
        main[0] if main else None,
        parallel,
        main[1] if len(main) > 1 else None,
        tuple(main[2:]),
        history,
    )


def read_project_roadmap_explorer(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    now: datetime | None = None,
) -> RoadmapExplorerProjection:
    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    scheduler = derive_scheduler_projection(roadmap.pipeline)
    mappings, mapping_diagnostics = parse_documented_issue_mappings(roadmap.issue.body)
    scheduler_by_key = {item.work_item.key: item for item in scheduler.items}

    items: list[RoadmapExplorerItem] = []
    for item in roadmap.pipeline.work_items:
        scheduler_item = scheduler_by_key.get(item.key)
        work_issue_number = mappings.get(item.key)
        parent_issue_number = int(item.parent.removeprefix("#"))
        items.append(RoadmapExplorerItem(
            key=item.key,
            title=item.title,
            type=item.type.value,
            status=item.status.value,
            lane=item.lane,
            parent=item.parent,
            replaces=item.replaces,
            depends_on=item.depends_on,
            unsatisfied_dependencies=(
                scheduler_item.unsatisfied_dependencies if scheduler_item is not None else ()
            ),
            scheduler_state=(
                scheduler_item.state.value if scheduler_item is not None else None
            ),
            scheduler_reason=(
                scheduler_item.reason.value if scheduler_item is not None else None
            ),
            expected_role=(scheduler_item.expected_role if scheduler_item is not None else None),
            next_action=(
                scheduler_item.next_action.value if scheduler_item is not None else None
            ),
            executable=(scheduler_item.executable if scheduler_item is not None else False),
            work_issue=(
                _issue_reference(project.repository_full_name, work_issue_number)
                if work_issue_number is not None
                else None
            ),
            parent_issue=_issue_reference(project.repository_full_name, parent_issue_number),
        ))

    now_key, parallel, next_key, later, history = _derive_horizons(roadmap.pipeline)
    return RoadmapExplorerProjection(
        project=project,
        observed_at=now or datetime.now(timezone.utc),
        roadmap_issue=_issue_reference(
            project.repository_full_name,
            project.roadmap_issue_number,
        ),
        roadmap_updated_at=roadmap.issue.updated_at,
        revision=sha256(roadmap.issue.body.encode("utf-8")).hexdigest(),
        pipeline_valid=roadmap.pipeline.valid,
        pipeline_version=roadmap.pipeline.version,
        pipeline_diagnostics=tuple(
            RoadmapExplorerDiagnostic(
                diagnostic.code,
                diagnostic.message,
                line_number=diagnostic.line_number,
            )
            for diagnostic in roadmap.pipeline.diagnostics
        ),
        scheduler_valid=scheduler.valid,
        scheduler_diagnostics=tuple(
            RoadmapExplorerDiagnostic(
                diagnostic.code,
                diagnostic.message,
                line_number=diagnostic.line_number,
            )
            for diagnostic in scheduler.diagnostics
        ),
        issue_mapping_diagnostics=mapping_diagnostics,
        items=tuple(items),
        now=now_key,
        parallel=parallel,
        next=next_key,
        later=later,
        history=history,
    )


def read_project_roadmap_issue_detail(
    project: Project,
    issue_number: int,
    *,
    roadmap_reader: RoadmapIssueReader,
    issue_reader: RoadmapExplorerIssueReader,
) -> RoadmapExplorerIssueDetail:
    if issue_number < 1:
        raise ValueError("issue_number must be positive")

    explorer = read_project_roadmap_explorer(project, roadmap_reader=roadmap_reader)
    referenced = {explorer.roadmap_issue.number}
    for item in explorer.items:
        if item.parent_issue is not None:
            referenced.add(item.parent_issue.number)
        if item.work_issue is not None:
            referenced.add(item.work_issue.number)
    if issue_number not in referenced:
        raise RoadmapExplorerIssueNotReferencedError(
            f"GitHub issue #{issue_number} is not referenced by the current roadmap explorer projection."
        )
    return issue_reader.read(project.repository_full_name, issue_number)
