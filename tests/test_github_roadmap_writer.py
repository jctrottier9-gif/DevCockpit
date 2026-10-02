import httpx
import pytest

from app.application.roadmap_changes import (
    RoadmapWriteNotEmittedError,
    RoadmapWriteUncertainError,
)
from app.application.roadmaps import RoadmapSourceError
from app.domain.project import Project
from app.infrastructure.github_roadmap_writer import (
    GitHubIssueMappingReader,
    GitHubRoadmapWriter,
)


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


def test_writer_patches_only_configured_roadmap_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = request.content.decode()
        return httpx.Response(200, json={"body": "after"})

    writer = GitHubRoadmapWriter(
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    writer.write(PROJECT, "after")

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/repos/jctrottier9-gif/DevCockpit/issues/1"
    assert seen["json"] == '{"body":"after"}'


@pytest.mark.parametrize("status", [300, 400, 401, 403, 404, 409, 422])
def test_writer_http_rejection_is_proven_not_applied(status):
    writer = GitHubRoadmapWriter(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    )
    with pytest.raises(RoadmapWriteNotEmittedError):
        writer.write(PROJECT, "after")


def test_writer_network_failure_is_uncertain():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection dropped", request=request)

    writer = GitHubRoadmapWriter(transport=httpx.MockTransport(handler))
    with pytest.raises(RoadmapWriteUncertainError):
        writer.write(PROJECT, "after")


def test_issue_mapping_reader_only_checks_existing_issue():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issues/28"):
            return httpx.Response(200, json={"number": 28})
        return httpx.Response(404)

    reader = GitHubIssueMappingReader(transport=httpx.MockTransport(handler))
    assert reader.exists(PROJECT.repository_full_name, 28) is True
    assert reader.exists(PROJECT.repository_full_name, 999) is False


def test_issue_mapping_reader_unavailability_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    reader = GitHubIssueMappingReader(transport=httpx.MockTransport(handler))
    with pytest.raises(RoadmapSourceError):
        reader.exists(PROJECT.repository_full_name, 28)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_writer_server_failure_is_uncertain(status):
    writer = GitHubRoadmapWriter(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    )
    with pytest.raises(RoadmapWriteUncertainError):
        writer.write(PROJECT, "after")
