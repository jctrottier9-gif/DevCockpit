from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.application.delivery_contexts import dev_target_instructions, release_automation_allowed
from app.domain.delivery_context import (
    CompletionPolicy, DeliveryCompletionEvidence, DeliveryContext,
    DeliveryContractError, DeliveryMode, DeliveryObservation, ReferenceKind,
    can_reconcile_done, validate_observation, validate_pair,
)
from app.domain.project import Project, ProjectConfigurationError
from app.infrastructure.database import Base
from app.infrastructure.delivery_contexts import (
    DeliveryContextRecord, SqlAlchemyDeliveryContextRepository,
)


def normal() -> DeliveryContext:
    return DeliveryContext(
        schema_version=1, repository_id=5, repository_full_name="owner/repo",
        work_item_id="FIX-1", delivery_issue_number=41,
        accepted_issue_body_sha256=sha256(b"accepted").hexdigest(),
        mode=DeliveryMode.NORMAL, requested_ref="main",
        ref_kind=ReferenceKind.BRANCH, resolved_ref="refs/heads/main",
        source_sha="a" * 40, expected_pr_base="main",
        observed_pr_base_sha="a" * 40, expected_work_branch="dev/FIX-1",
        starting_sha="a" * 40,
    )


def hotfix() -> DeliveryContext:
    return replace(
        normal(), mode=DeliveryMode.HOTFIX,
        requested_ref="release/1.4", resolved_ref="refs/heads/release/1.4",
        expected_pr_base="release/1.4", correction_id="CORR-1",
        linked_work_item="FWD-1", release_id="1.4",
        release_branch="release/1.4", release_origin_sha="0" * 40,
        release_state="MAINTAINED",
        completion_policy=CompletionPolicy.VERIFIED_ARTIFACT,
    )


def forward_port() -> DeliveryContext:
    return replace(
        normal(), work_item_id="FWD-1", mode=DeliveryMode.FORWARD_PORT,
        correction_id="CORR-1", linked_work_item="FIX-1",
        source_hotfix_pr=123, integrated_commits=("b" * 40,),
        forward_port_method="CHERRY_PICK_INTEGRATED",
    )


def obs(**changes) -> DeliveryObservation:
    return replace(
        DeliveryObservation(
            repository_id=5, repository_full_name="owner/repo",
            resolved_ref="refs/heads/main", source_commit_sha="a" * 40,
            current_base_sha="a" * 40,
        ),
        **changes,
    )


def test_versioned_contract_requires_explicit_mode_and_full_refs():
    assert DeliveryContext.from_json(normal().canonical_json()) == normal()
    assert normal().fingerprint() == sha256(normal().canonical_json().encode()).hexdigest()
    with pytest.raises(DeliveryContractError):
        replace(normal(), mode="MAYBE")
    with pytest.raises(DeliveryContractError):
        replace(normal(), source_sha="abc123")
    with pytest.raises(DeliveryContractError):
        replace(normal(), requested_ref="refs/heads/main")
    with pytest.raises(DeliveryContractError):
        replace(normal(), resolved_ref="refs/heads/release/other")
    with pytest.raises(DeliveryContractError):
        replace(normal(), schema_version=99)
    with pytest.raises(DeliveryContractError):
        DeliveryContext.from_json('{"mode":"NORMAL"}')


def test_hotfix_never_falls_back_to_main_and_forward_port_requires_proven_source():
    assert hotfix().expected_pr_base == "release/1.4"
    assert hotfix().ref_kind is ReferenceKind.BRANCH
    validate_pair(hotfix(), forward_port())
    with pytest.raises(DeliveryContractError):
        replace(hotfix(), expected_pr_base="main")
    with pytest.raises(DeliveryContractError):
        replace(hotfix(), requested_ref="main", resolved_ref="refs/heads/main")
    with pytest.raises(DeliveryContractError):
        replace(hotfix(), linked_work_item="")
    with pytest.raises(DeliveryContractError):
        replace(hotfix(), release_state="RETIRED")
    with pytest.raises(DeliveryContractError):
        replace(forward_port(), integrated_commits=())
    with pytest.raises(DeliveryContractError):
        validate_pair(hotfix(), replace(forward_port(), correction_id="other"))


def test_reference_revalidation_fails_closed_on_moved_or_missing_evidence():
    assert validate_observation(normal(), obs()) == ()
    assert "SOURCE_MOVED_OR_MISSING" in validate_observation(
        normal(), obs(source_commit_sha="c" * 40))
    assert "BASE_TIP_STALE" in validate_observation(
        normal(), obs(current_base_sha="c" * 40))
    assert "REPOSITORY_MISMATCH" in validate_observation(
        normal(), obs(repository_id=6))
    assert "REF_MISMATCH" in validate_observation(
        normal(), obs(resolved_ref="refs/tags/v1.4"))
    assert "WORK_BRANCH_MISSING" in validate_observation(
        normal(), obs(working_branch="dev/FIX-1", working_head_sha=None))
    assert "PR_RETARGETED" in validate_observation(
        normal(), obs(pr_base_name="release/1.4"))
    # Even if release and main point to an identical tip, names are authoritative.
    assert "PR_RETARGETED" in validate_observation(
        normal(), obs(pr_base_name="release/1.4", current_base_sha="a" * 40))


def test_hotfix_completion_policy_distinguishes_code_artifact_validation_and_deploy():
    ctx = hotfix()
    merged = DeliveryCompletionEvidence(merged=True, green_current_head=True)
    assert not can_reconcile_done(ctx, merged)
    assert can_reconcile_done(normal(), merged)
    artifact = replace(merged, published_tag="v1.4.1",
                       published_source_sha="c" * 40,
                       image_digest="sha256:" + "d" * 64)
    assert not can_reconcile_done(ctx, artifact)
    assert can_reconcile_done(ctx, replace(artifact, validated=True))
    assert not replace(artifact, validated=True).deployed
    assert not can_reconcile_done(ctx, replace(artifact, published_source_sha=None, validated=True))


def test_legacy_normal_prompt_and_release_automation_gate():
    project = Project("Example", "owner/repo", 1, delivery_contexts=(normal(),))
    assert project.delivery_context_for("FIX-1") == normal()
    assert project.delivery_context_for("other") is None
    assert release_automation_allowed(project.delivery_context_for("other"))
    assert release_automation_allowed(normal())
    assert release_automation_allowed(hotfix())  # DC-075B guarded path
    assert release_automation_allowed(forward_port())  # DC-075C guarded path
    assert "vrai main" in dev_target_instructions(None)
    prompt = dev_target_instructions(hotfix())
    assert "release/1.4" in prompt and "refs/heads/release/1.4" in prompt
    assert "repository_id=5" in prompt and "SHA" in prompt
    with pytest.raises(ProjectConfigurationError):
        Project("Example", "other/repo", 1, delivery_contexts=(hotfix(),))
    with pytest.raises(ProjectConfigurationError):
        Project("Example", "owner/repo", 1, delivery_contexts=(normal(), normal()))


def test_additive_accepted_snapshot_is_immutable_and_corruption_is_detected():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = SqlAlchemyDeliveryContextRepository(session)
        repo.add_accepted(hotfix())
        session.commit()
        assert repo.get(5, "FIX-1") == hotfix()
        repo.add_accepted(hotfix())  # idempotent exact replay
        with pytest.raises(DeliveryContractError):
            repo.add_accepted(replace(hotfix(), observed_pr_base_sha="b" * 40))
        row = session.get(DeliveryContextRecord, (5, "FIX-1"))
        row.contract_sha256 = "f" * 64
        session.flush()
        with pytest.raises(DeliveryContractError):
            repo.get(5, "FIX-1")
    engine.dispose()
