from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.roadmap_changes import (
    RoadmapWriteNotEmittedError,
    RoadmapWriteUncertainError,
)
from app.application.roadmaps import RoadmapSourceError
from app.domain.project import Project


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DevCockpit",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _issue_url(repository_full_name: str, issue_number: int) -> str:
    owner, repository = repository_full_name.split("/", maxsplit=1)
    return (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/issues/{int(issue_number)}"
    )


class GitHubRoadmapWriter:
    """Narrow GitHub adapter that can only replace the configured roadmap issue body."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def write(self, project: Project, body: str) -> None:
        url = _issue_url(project.repository_full_name, project.roadmap_issue_number)
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.patch(
                    url,
                    headers=_headers(self._token),
                    json={"body": body},
                )
        except httpx.RequestError as exc:
            raise RoadmapWriteUncertainError(
                "GitHub PATCH outcome is uncertain; explicit reconciliation is required"
            ) from exc

        if response.status_code >= 500:
            raise RoadmapWriteUncertainError(
                f"GitHub returned HTTP {response.status_code} after PATCH; "
                "the remote outcome is uncertain"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise RoadmapWriteNotEmittedError(
                f"GitHub definitively rejected roadmap body PATCH with HTTP {response.status_code}"
            )


class GitHubIssueMappingReader:
    """Read-only existence check for issue mappings required by a roadmap proposal."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def exists(self, repository_full_name: str, issue_number: int) -> bool:
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    _issue_url(repository_full_name, issue_number),
                    headers=_headers(self._token),
                )
        except httpx.RequestError as exc:
            raise RoadmapSourceError("GitHub issue mapping request failed") from exc

        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise RoadmapSourceError(
                f"GitHub issue mapping request failed with HTTP {response.status_code}"
            )
        return True
