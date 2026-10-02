from __future__ import annotations

from dataclasses import dataclass

from app.application.roadmaps import RoadmapIssue, RoadmapIssueReader, read_project_roadmap
from app.domain.project import Project
from app.domain.scheduler import SchedulerProjection, derive_scheduler_projection


@dataclass(frozen=True, slots=True)
class ProjectSchedulerProjection:
    project: Project
    issue: RoadmapIssue
    scheduler: SchedulerProjection


def read_project_scheduler(
    project: Project,
    *,
    reader: RoadmapIssueReader,
) -> ProjectSchedulerProjection:
    roadmap = read_project_roadmap(project, reader=reader)
    return ProjectSchedulerProjection(
        project=project,
        issue=roadmap.issue,
        scheduler=derive_scheduler_projection(roadmap.pipeline),
    )
