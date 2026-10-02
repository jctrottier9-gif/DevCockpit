from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.roadmaps import RoadmapIssue, RoadmapIssueReader, read_project_roadmap
from app.domain.flow_analytics import (
    FlowAnalyticsEvidence,
    FlowAnalyticsProjection,
    FlowDiagnostic,
    derive_flow_analytics,
)
from app.domain.project import Project
from app.domain.roadmap import WorkItem, WorkItemType


class FlowAnalyticsSourceError(RuntimeError):
    code = "GITHUB_UNAVAILABLE"


class FlowAnalyticsAuthorizationError(FlowAnalyticsSourceError):
    code = "GITHUB_AUTHORIZATION_FAILED"


class FlowAnalyticsRepositoryNotFoundError(FlowAnalyticsSourceError):
    code = "GITHUB_REPOSITORY_NOT_FOUND"


class FlowAnalyticsPayloadError(FlowAnalyticsSourceError):
    code = "GITHUB_PAYLOAD_INVALID"


class FlowAnalyticsEvidenceReader(Protocol):
    def read(
        self,
        project: Project,
        work_items: tuple[WorkItem, ...],
    ) -> FlowAnalyticsEvidence: ...


@dataclass(frozen=True, slots=True)
class ProjectFlowAnalyticsProjection:
    project: Project
    issue: RoadmapIssue
    analytics: FlowAnalyticsProjection


def read_project_flow_analytics(
    project: Project,
    *,
    roadmap_reader: RoadmapIssueReader,
    analytics_reader: FlowAnalyticsEvidenceReader,
) -> ProjectFlowAnalyticsProjection:
    roadmap = read_project_roadmap(project, reader=roadmap_reader)
    if not roadmap.pipeline.valid:
        projection = derive_flow_analytics(
            FlowAnalyticsEvidence(
                diagnostics=tuple(
                    FlowDiagnostic(item.code, item.message)
                    for item in roadmap.pipeline.diagnostics
                )
            )
        )
        return ProjectFlowAnalyticsProjection(project, roadmap.issue, projection)

    work_items = tuple(
        item
        for item in roadmap.pipeline.work_items
        if item.type is WorkItemType.WORK
    )
    evidence = analytics_reader.read(project, work_items)
    return ProjectFlowAnalyticsProjection(
        project=project,
        issue=roadmap.issue,
        analytics=derive_flow_analytics(evidence),
    )
