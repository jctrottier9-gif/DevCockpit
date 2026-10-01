from __future__ import annotations

from collections.abc import Iterable

from app.domain.project import Project, ProjectConfigurationError


class ProjectCatalog:
    def __init__(self, projects: Iterable[Project]) -> None:
        project_list = tuple(projects)
        by_id = {project.project_id: project for project in project_list}
        if len(by_id) != len(project_list):
            raise ProjectConfigurationError("project_id values must be unique")
        if not project_list:
            raise ProjectConfigurationError("at least one Project must be configured")
        self._projects = project_list
        self._by_id = by_id

    def list(self) -> tuple[Project, ...]:
        return self._projects

    def get(self, project_id: str) -> Project | None:
        return self._by_id.get(project_id)
