from __future__ import annotations

from dataclasses import dataclass
import re


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

    def __post_init__(self) -> None:
        if not _PROJECT_ID_PATTERN.fullmatch(self.project_id):
            raise ProjectConfigurationError("project_id must be a stable logical identifier")
        if not _REPOSITORY_PATTERN.fullmatch(self.repository_full_name):
            raise ProjectConfigurationError("repository_full_name must use owner/name form")
        if self.roadmap_issue_number <= 0:
            raise ProjectConfigurationError("roadmap_issue_number must be greater than zero")
