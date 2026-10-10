from __future__ import annotations

import json
from pathlib import Path

from app.domain.delivery_context import DeliveryContext
from app.domain.project import Project, ProjectConfigurationError
from app.domain.resource_lock import (
    ResourceLockRequirement,
    WorkItemResourceLockDeclaration,
)


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
        required_keys = {
            "project_id",
            "repository_full_name",
            "roadmap_issue_number",
        }
        allowed_keys = required_keys | {"resource_locks", "delivery_contexts"}
        if (
            not isinstance(item, dict)
            or not required_keys <= set(item)
            or not set(item) <= allowed_keys
        ):
            raise ProjectConfigurationError(f"Project entry {index} has an invalid shape")
        try:
            project = Project(
                project_id=item["project_id"],
                repository_full_name=item["repository_full_name"],
                roadmap_issue_number=item["roadmap_issue_number"],
                delivery_contexts=_parse_delivery_contexts(item.get("delivery_contexts", [])),
                resource_locks=_parse_resource_locks(
                    item.get("resource_locks", {}),
                    project_index=index,
                ),
            )
        except (TypeError, ValueError, ProjectConfigurationError) as exc:
            raise ProjectConfigurationError(f"Project entry {index} is invalid: {exc}") from exc
        if project.project_id in seen_ids:
            raise ProjectConfigurationError(f"Duplicate project_id: {project.project_id}")
        seen_ids.add(project.project_id)
        projects.append(project)

    if not projects:
        raise ProjectConfigurationError("At least one Project must be configured")
    return tuple(projects)


def _parse_resource_locks(
    payload: object,
    *,
    project_index: int,
) -> tuple[WorkItemResourceLockDeclaration, ...]:
    if not isinstance(payload, dict):
        raise ProjectConfigurationError(
            f"Project entry {project_index} resource_locks must be an object"
        )

    declarations: list[WorkItemResourceLockDeclaration] = []
    for work_item_id, raw_requirements in payload.items():
        if not isinstance(work_item_id, str) or not isinstance(raw_requirements, list):
            raise ProjectConfigurationError(
                f"Project entry {project_index} resource_locks has an invalid WorkItem entry"
            )

        requirements: list[ResourceLockRequirement] = []
        for raw_requirement in raw_requirements:
            if (
                not isinstance(raw_requirement, dict)
                or set(raw_requirement) != {"surface", "mode"}
                or not isinstance(raw_requirement["surface"], str)
                or not isinstance(raw_requirement["mode"], str)
            ):
                raise ProjectConfigurationError(
                    f"Project entry {project_index} ResourceLock requirement has an invalid shape"
                )
            requirements.append(
                ResourceLockRequirement.build(
                    raw_requirement["surface"],
                    raw_requirement["mode"],
                )
            )

        declarations.append(
            WorkItemResourceLockDeclaration(
                work_item_id=work_item_id,
                requirements=tuple(requirements),
            )
        )
    return tuple(declarations)

def _parse_delivery_contexts(payload: object) -> tuple[DeliveryContext, ...]:
    if not isinstance(payload, list):
        raise ProjectConfigurationError("delivery_contexts must be a list")
    try:
        import json
        return tuple(DeliveryContext.from_json(json.dumps(entry)) for entry in payload)
    except (TypeError, ValueError) as exc:
        raise ProjectConfigurationError(f"Invalid delivery context: {exc}") from exc
