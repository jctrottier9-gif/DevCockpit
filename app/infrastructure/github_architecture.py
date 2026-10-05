from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote

import httpx

from app.application.cockpit_panels import (
    ArchitectureDocumentDetail,
    ArchitectureDocumentError,
    ArchitectureDocumentNotFound,
    ArchitectureDocumentReference,
)


_ADR_FILE = re.compile(r"^(ADR-[0-9]{4})-(.+)\.md$")


class GitHubArchitectureDocumentReader:
    """Read explicitly referenced ADR documents from the configured repository on demand."""

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

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DevCockpit",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _contents_url(self, repository_full_name: str, path: str) -> str:
        owner, repository = repository_full_name.split("/", maxsplit=1)
        return (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repository, safe='')}/contents/"
            f"{quote(path, safe='/')}"
        )

    def _get(self, repository_full_name: str, path: str) -> object:
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers=self._headers(),
            ) as client:
                response = client.get(self._contents_url(repository_full_name, path))
        except httpx.RequestError as exc:
            raise ArchitectureDocumentError("GitHub architecture document request failed") from exc

        if response.status_code == 404:
            raise ArchitectureDocumentNotFound(path)
        if response.status_code in {401, 403}:
            raise ArchitectureDocumentError("GitHub rejected architecture document access")
        if response.status_code >= 400:
            raise ArchitectureDocumentError(
                f"GitHub architecture document request failed with HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ArchitectureDocumentError(
                "GitHub architecture document response was not valid JSON"
            ) from exc

    def list(self, repository_full_name: str) -> tuple[ArchitectureDocumentReference, ...]:
        payload = self._get(repository_full_name, "docs/architecture")
        if not isinstance(payload, list):
            raise ArchitectureDocumentError("GitHub architecture directory must be an array")

        references: list[ArchitectureDocumentReference] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            url = item.get("html_url")
            if not isinstance(name, str) or not isinstance(path, str) or not isinstance(url, str):
                continue
            match = _ADR_FILE.fullmatch(name)
            if match is None:
                continue
            title = match.group(2).replace("-", " ")
            references.append(
                ArchitectureDocumentReference(
                    adr_id=match.group(1),
                    title=title,
                    path=path,
                    url=url,
                )
            )
        return tuple(sorted(references, key=lambda item: item.adr_id))

    def read(
        self,
        repository_full_name: str,
        path: str,
    ) -> ArchitectureDocumentDetail:
        payload = self._get(repository_full_name, path)
        if not isinstance(payload, dict):
            raise ArchitectureDocumentError("GitHub architecture document must be an object")
        name = payload.get("name")
        encoded = payload.get("content")
        encoding = payload.get("encoding")
        url = payload.get("html_url")
        if not all(isinstance(value, str) for value in (name, encoded, encoding, url)):
            raise ArchitectureDocumentError("GitHub architecture document payload is incomplete")
        match = _ADR_FILE.fullmatch(name)
        if match is None:
            raise ArchitectureDocumentNotFound(path)
        if encoding != "base64":
            raise ArchitectureDocumentError("GitHub architecture document encoding is unsupported")
        try:
            content = base64.b64decode(encoded).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise ArchitectureDocumentError("GitHub architecture document content is invalid") from exc
        reference = ArchitectureDocumentReference(
            adr_id=match.group(1),
            title=match.group(2).replace("-", " "),
            path=path,
            url=url,
        )
        return ArchitectureDocumentDetail(reference=reference, content=content)
