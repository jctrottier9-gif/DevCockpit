from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.roadmap_explorer import (
    RoadmapExplorerIssueAuthorizationError,
    RoadmapExplorerIssueDetail,
    RoadmapExplorerIssueError,
    RoadmapExplorerIssueNotFoundError,
    RoadmapExplorerIssuePayloadError,
)


def _issue_url(repository_full_name: str, issue_number: int) -> str:
    owner, repository = repository_full_name.split("/", maxsplit=1)
    return (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/issues/{int(issue_number)}"
    )


class GitHubIssueReader:
    """Read GitHub issue details on demand for cockpit navigation."""

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

    def read(self, repository_full_name: str, issue_number: int) -> RoadmapExplorerIssueDetail:
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
                response = client.get(
                    _issue_url(repository_full_name, issue_number),
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise RoadmapExplorerIssueError("GitHub issue request failed") from exc

        if response.status_code in {401, 403}:
            raise RoadmapExplorerIssueAuthorizationError("GitHub rejected issue access")
        if response.status_code == 404:
            raise RoadmapExplorerIssueNotFoundError("GitHub issue was not found")
        if response.status_code >= 400:
            raise RoadmapExplorerIssueError(
                f"GitHub issue request failed with HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoadmapExplorerIssuePayloadError(
                "GitHub issue response was not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RoadmapExplorerIssuePayloadError("GitHub issue response must be an object")

        title = payload.get("title")
        body = payload.get("body")
        state = payload.get("state")
        html_url = payload.get("html_url")
        updated_at = payload.get("updated_at")
        if body is None:
            body = ""
        if not all(isinstance(value, str) for value in (title, body, state, html_url)):
            raise RoadmapExplorerIssuePayloadError(
                "GitHub issue title, body, state and html_url must be text"
            )
        if updated_at is not None and not isinstance(updated_at, str):
            raise RoadmapExplorerIssuePayloadError(
                "GitHub issue updated_at must be text or null"
            )

        return RoadmapExplorerIssueDetail(
            repository_full_name=repository_full_name,
            number=issue_number,
            title=title,
            body=body,
            state=state,
            url=html_url,
            updated_at=updated_at,
        )
