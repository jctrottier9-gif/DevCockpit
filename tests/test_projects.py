import json
from pathlib import Path

import pytest

from app.application.projects import ProjectCatalog
from app.domain.project import Project, ProjectConfigurationError
from app.infrastructure.project_config import load_projects


def test_load_projects_supports_multiple_explicit_projects(tmp_path: Path) -> None:
    path = tmp_path / "projects.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "project_id": "DevCockpit",
                        "repository_full_name": "jctrottier9-gif/DevCockpit",
                        "roadmap_issue_number": 1,
                    },
                    {
                        "project_id": "RessourcePlanner",
                        "repository_full_name": "tchi99/RessourcePlanner",
                        "roadmap_issue_number": 55,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    projects = load_projects(path)
    catalog = ProjectCatalog(projects)

    assert [project.project_id for project in catalog.list()] == ["DevCockpit", "RessourcePlanner"]
    assert catalog.get("RessourcePlanner") == projects[1]


def test_project_identity_is_not_derived_from_issue_or_repository() -> None:
    project = Project("StableProject", "owner/repository", 42)

    assert project.project_id == "StableProject"
    assert project.roadmap_issue_number == 42


@pytest.mark.parametrize(
    "project",
    [
        lambda: Project("bad id", "owner/repo", 1),
        lambda: Project("Project", "not-a-repository", 1),
        lambda: Project("Project", "owner/repo", 0),
    ],
)
def test_invalid_project_configuration_is_rejected(project) -> None:
    with pytest.raises(ProjectConfigurationError):
        project()



def test_project_config_parses_explicit_resource_lock_declarations(tmp_path: Path) -> None:
    path = tmp_path / "projects.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "project_id": "DevCockpit",
                        "repository_full_name": "jctrottier9-gif/DevCockpit",
                        "roadmap_issue_number": 1,
                        "resource_locks": {
                            "DC-052": [
                                {
                                    "surface": "migration:alembic",
                                    "mode": "EXCLUSIVE",
                                },
                                {
                                    "surface": "api:contracts",
                                    "mode": "SHARED",
                                },
                            ]
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    project = load_projects(path)[0]
    requirements = project.resource_lock_requirements_for("DC-052")

    assert [(item.surface.key, item.mode.value) for item in requirements] == [
        ("migration:alembic", "EXCLUSIVE"),
        ("api:contracts", "SHARED"),
    ]
    assert project.resource_lock_requirements_for("DC-060") == ()
