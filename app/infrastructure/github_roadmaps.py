from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.roadmaps import (
    RoadmapAuthorizationError,
    RoadmapIssue,
    RoadmapIssueNotFoundError,
    RoadmapPayloadError,
    RoadmapSourceError,
)
from app.domain.project import Project


class GitHubRoadmapReader:
    """Read-only adapter for fetching the configured GitHub roadmap issue."""

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

    def read(self, project: Project) -> RoadmapIssue:
        owner, repository = project.repository_full_name.split("/", maxsplit=1)
        url = (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repository, safe='')}/issues/{project.roadmap_issue_number}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DevCockpit",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise RoadmapSourceError("GitHub roadmap request failed") from exc

        if response.status_code in {401, 403}:
            raise RoadmapAuthorizationError("GitHub rejected roadmap access")
        if response.status_code == 404:
            raise RoadmapIssueNotFoundError("Configured GitHub roadmap issue was not found")
        if response.status_code >= 400:
            raise RoadmapSourceError(f"GitHub roadmap request failed with HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoadmapPayloadError("GitHub roadmap response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise RoadmapPayloadError("GitHub roadmap response must be an object")

        body = payload.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise RoadmapPayloadError("GitHub roadmap body must be text or null")
        updated_at = payload.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise RoadmapPayloadError("GitHub roadmap updated_at must be text or null")

        return RoadmapIssue(
            repository_full_name=project.repository_full_name,
            issue_number=project.roadmap_issue_number,
            body=body,
            updated_at=updated_at,
        )
