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

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        token_configured: bool | None = None,
        rate_limit_remaining: str | None = None,
        rate_limit_reset: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.token_configured = token_configured
        self.rate_limit_remaining = rate_limit_remaining
        self.rate_limit_reset = rate_limit_reset
        self.retry_after = retry_after

    def source_details(self) -> dict[str, object]:
        details: dict[str, object] = {}
        if self.status_code is not None:
            details["http_status"] = self.status_code
        if self.token_configured is not None:
            details["token_configured"] = self.token_configured
        if self.rate_limit_remaining is not None:
            details["rate_limit_remaining"] = self.rate_limit_remaining
        if self.rate_limit_reset is not None:
            details["rate_limit_reset"] = self.rate_limit_reset
        if self.retry_after is not None:
            details["retry_after"] = self.retry_after
        return details


class RoadmapAuthorizationError(RoadmapSourceError):
    code = "GITHUB_AUTHORIZATION_FAILED"


class RoadmapRateLimitError(RoadmapSourceError):
    code = "GITHUB_RATE_LIMITED"


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
