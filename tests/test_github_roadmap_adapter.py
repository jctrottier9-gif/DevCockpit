import httpx
import pytest

from app.application.roadmaps import (
    RoadmapAuthorizationError,
    RoadmapIssueNotFoundError,
    RoadmapPayloadError,
    RoadmapSourceError,
)
from app.domain.project import Project
from app.infrastructure.github_roadmaps import GitHubRoadmapReader


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


def reader_for(handler, *, token: str | None = None) -> GitHubRoadmapReader:
    return GitHubRoadmapReader(token=token, transport=httpx.MockTransport(handler))


def test_reads_issue_with_optional_server_side_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/jctrottier9-gif/DevCockpit/issues/1"
        assert request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(200, json={"body": "roadmap body", "updated_at": "2026-10-01T12:00:00Z"})

    issue = reader_for(handler, token="secret-token").read(PROJECT)

    assert issue.repository_full_name == PROJECT.repository_full_name
    assert issue.issue_number == 1
    assert issue.body == "roadmap body"
    assert issue.updated_at == "2026-10-01T12:00:00Z"


def test_null_body_is_returned_as_empty_text_for_parser_diagnostics() -> None:
    reader = reader_for(lambda _: httpx.Response(200, json={"body": None, "updated_at": None}))

    assert reader.read(PROJECT).body == ""


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_errors_are_typed(status: int) -> None:
    reader = reader_for(lambda _: httpx.Response(status))

    with pytest.raises(RoadmapAuthorizationError):
        reader.read(PROJECT)


def test_missing_issue_is_typed() -> None:
    reader = reader_for(lambda _: httpx.Response(404))

    with pytest.raises(RoadmapIssueNotFoundError):
        reader.read(PROJECT)


def test_network_error_is_not_converted_to_empty_pipeline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(RoadmapSourceError):
        reader_for(handler).read(PROJECT)


def test_malformed_payload_is_typed() -> None:
    reader = reader_for(lambda _: httpx.Response(200, json={"body": 123}))

    with pytest.raises(RoadmapPayloadError):
        reader.read(PROJECT)
