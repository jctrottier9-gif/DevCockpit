from __future__ import annotations

import json
from pathlib import Path

from app.domain.project import Project, ProjectConfigurationError


def load_projects(path: str | Path) -> tuple[Project, ...]:
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectConfigurationError(f"Project configuration file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectConfigurationError(f"Project configuration is not valid JSON: {config_path}") from exc

    if not isinstance(payload, dict) or set(payload) != {"projects"} or not isinstance(payload["projects"], list):
        raise ProjectConfigurationError("Project configuration must contain exactly one projects array")

    projects: list[Project] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(payload["projects"]):
        if not isinstance(item, dict) or set(item) != {
            "project_id",
            "repository_full_name",
            "roadmap_issue_number",
        }:
            raise ProjectConfigurationError(f"Project entry {index} has an invalid shape")
        try:
            project = Project(
                project_id=item["project_id"],
                repository_full_name=item["repository_full_name"],
                roadmap_issue_number=item["roadmap_issue_number"],
            )
        except (TypeError, ProjectConfigurationError) as exc:
            raise ProjectConfigurationError(f"Project entry {index} is invalid: {exc}") from exc
        if project.project_id in seen_ids:
            raise ProjectConfigurationError(f"Duplicate project_id: {project.project_id}")
        seen_ids.add(project.project_id)
        projects.append(project)

    if not projects:
        raise ProjectConfigurationError("At least one Project must be configured")
    return tuple(projects)
