from dataclasses import replace

import httpx
import pytest

from app.domain.delivery_context import DeliveryMode, ReferenceKind
from app.infrastructure.github_delivery_context import (
    DeliveryReferenceError, GitHubDeliveryReferenceReader,
)
from tests.test_delivery_context import hotfix, normal


def github_transport(*, base_sha="a" * 40, source_sha="a" * 40,
                     pr_base="main", missing_release=False, moved_tag=False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 5, "full_name": "owner/repo"})
        if path == "/repos/owner/repo/issues/41":
            return httpx.Response(200, json={"body": "accepted"})
        if path == "/repos/owner/repo/git/ref/heads/main":
            return httpx.Response(200, json={
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": base_sha},
            })
        if path == "/repos/owner/repo/git/ref/heads/release/1.4":
            if missing_release:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={
                "ref": "refs/heads/release/1.4",
                "object": {"type": "commit", "sha": source_sha},
            })
        if path == "/repos/owner/repo/git/ref/heads/dev/FIX-1":
            return httpx.Response(200, json={
                "ref": "refs/heads/dev/FIX-1",
                "object": {"type": "commit", "sha": "d" * 40},
            })
        if path == "/repos/owner/repo/pulls/11":
            return httpx.Response(200, json={
                "base": {"ref": pr_base},
                "head": {"ref": "dev/FIX-1", "repo": {"id": 5}},
            })
        if path == "/repos/owner/repo/git/ref/tags/v1.4.0":
            return httpx.Response(200, json={
                "ref": "refs/tags/v1.4.0",
                "object": {"type": "tag", "sha": "e" * 40},
            })
        if path == "/repos/owner/repo/git/tags/" + "e" * 40:
            return httpx.Response(200, json={
                "object": {"type": "commit", "sha": "c" * 40 if moved_tag else "a" * 40},
            })
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.MockTransport(handler)


def test_main_ref_and_current_pr_base_are_proved_from_github():
    ctx = normal()
    observed = GitHubDeliveryReferenceReader(
        transport=github_transport()).read_verified(ctx, pr_number=11)
    assert observed.source_commit_sha == "a" * 40
    assert observed.pr_base_name == "main"
    assert observed.working_head_sha == "d" * 40


def test_main_moves_between_acceptance_and_dispatch_fails_closed():
    with pytest.raises(DeliveryReferenceError, match="SOURCE_MOVED_OR_MISSING"):
        GitHubDeliveryReferenceReader(
            transport=github_transport(base_sha="b" * 40)).read_verified(normal())


def test_retargeted_pr_fails_even_when_two_bases_have_same_tip():
    with pytest.raises(DeliveryReferenceError, match="PR_RETARGETED"):
        GitHubDeliveryReferenceReader(
            transport=github_transport(pr_base="release/1.4")).read_verified(
                normal(), pr_number=11)


def test_hotfix_missing_release_does_not_fallback_to_main():
    with pytest.raises(DeliveryReferenceError, match="unavailable"):
        GitHubDeliveryReferenceReader(
            transport=github_transport(missing_release=True)).read_verified(hotfix())


def test_moved_annotated_tag_is_rejected():
    ctx = replace(hotfix(), mode=DeliveryMode.RELEASE,
                  requested_ref="v1.4.0", ref_kind=ReferenceKind.TAG,
                  resolved_ref="refs/tags/v1.4.0",
                  completion_policy=hotfix().completion_policy)
    with pytest.raises(DeliveryReferenceError, match="SOURCE_MOVED_OR_MISSING"):
        GitHubDeliveryReferenceReader(
            transport=github_transport(moved_tag=True)).read_verified(ctx)


def test_absent_commit_sha_is_never_treated_as_short_ref():
    ctx = replace(normal(), ref_kind=ReferenceKind.SHA, requested_ref="a" * 40,
                  resolved_ref="a" * 40)
    with pytest.raises(DeliveryReferenceError, match="unavailable"):
        GitHubDeliveryReferenceReader(transport=github_transport()).read_verified(ctx)


def test_stale_delivery_issue_body_blocks_snapshot():
    def responder(request):
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 5, "full_name": "owner/repo"})
        return httpx.Response(200, json={"body": "changed"})
    with pytest.raises(DeliveryReferenceError, match="DELIVERY_ISSUE_CHANGED"):
        GitHubDeliveryReferenceReader(
            transport=httpx.MockTransport(responder)).read_verified(normal())
