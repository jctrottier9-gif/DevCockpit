from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.project import Project
from app.domain.roadmap import PipelineParseResult, parse_canonical_pipeline


@dataclass(frozen=True, slots=True)
class RoadmapIssue:
    repository_full_name: str
    issue_number: int
    body: str
    updated_at: str | None = None


class RoadmapSourceError(RuntimeError):
    code = "GITHUB_UNAVAILABLE"


class RoadmapAuthorizationError(RoadmapSourceError):
    code = "GITHUB_AUTHORIZATION_FAILED"


class RoadmapIssueNotFoundError(RoadmapSourceError):
    code = "GITHUB_ISSUE_NOT_FOUND"


class RoadmapPayloadError(RoadmapSourceError):
    code = "GITHUB_PAYLOAD_INVALID"


class RoadmapIssueReader(Protocol):
    def read(self, project: Project) -> RoadmapIssue: ...


@dataclass(frozen=True, slots=True)
class ProjectRoadmapProjection:
    project: Project
    issue: RoadmapIssue
    pipeline: PipelineParseResult


def read_project_roadmap(project: Project, *, reader: RoadmapIssueReader) -> ProjectRoadmapProjection:
    issue = reader.read(project)
    pipeline = parse_canonical_pipeline(issue.body)
    return ProjectRoadmapProjection(project=project, issue=issue, pipeline=pipeline)
