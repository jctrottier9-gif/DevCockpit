from __future__ import annotations

from dataclasses import dataclass
import re

from app.domain.delivery_context import DeliveryContext
from app.domain.resource_lock import (
    ResourceLockRequirement,
    WorkItemResourceLockDeclaration,
)


_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/[A-Za-z0-9_.-]+$"
)


class ProjectConfigurationError(ValueError):
    """Raised when a configured project is structurally invalid."""


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    repository_full_name: str
    roadmap_issue_number: int
    resource_locks: tuple[WorkItemResourceLockDeclaration, ...] = ()
    delivery_contexts: tuple[DeliveryContext, ...] = ()

    def __post_init__(self) -> None:
        if not _PROJECT_ID_PATTERN.fullmatch(self.project_id):
            raise ProjectConfigurationError("project_id must be a stable logical identifier")
        if not _REPOSITORY_PATTERN.fullmatch(self.repository_full_name):
            raise ProjectConfigurationError("repository_full_name must use owner/name form")
        if self.roadmap_issue_number <= 0:
            raise ProjectConfigurationError("roadmap_issue_number must be greater than zero")
        identities = [declaration.work_item_id for declaration in self.resource_locks]
        if len(identities) != len(set(identities)):
            raise ProjectConfigurationError(
                "resource_locks must contain at most one declaration per WorkItem"
            )

        context_ids = [context.work_item_id for context in self.delivery_contexts]
        if len(context_ids) != len(set(context_ids)):
            raise ProjectConfigurationError("Duplicate WorkItem delivery context")
        if any(context.repository_full_name != self.repository_full_name
               for context in self.delivery_contexts):
            raise ProjectConfigurationError("Delivery context repository does not match Project")

    def delivery_context_for(self, work_item_id: str) -> DeliveryContext | None:
        return next((context for context in self.delivery_contexts
                     if context.work_item_id == work_item_id), None)

    def resource_lock_requirements_for(
        self,
        work_item_id: str,
    ) -> tuple[ResourceLockRequirement, ...]:
        for declaration in self.resource_locks:
            if declaration.work_item_id == work_item_id:
                return declaration.requirements
        return ()
