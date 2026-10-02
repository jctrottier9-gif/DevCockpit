from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.roadmaps import (
    RoadmapIssueReader,
    RoadmapSourceError,
    read_project_roadmap,
)
from app.domain.handoff import (
    DecisionEffect,
    DecisionType,
    HandoffPurpose,
    OrchestrationConflict,
    utc_now,
)
from app.domain.project import Project
from app.domain.roadmap import parse_canonical_pipeline
from app.domain.roadmap_change import (
    GENERATOR_VERSION,
    VALIDATION_VERSION,
    ApplicationAttemptOutcome,
    ApplicationStatus,
    ProposalStatus,
    RoadmapChangeApplication,
    RoadmapChangeApplicationAttempt,
    RoadmapChangeProposal,
    RoadmapChangeProposalRevision,
    body_hash,
    build_preview,
    canonical_operations,
    generate_proposed_body,
    operations_json,
    parse_issue_mappings,
)


class RoadmapWriteNotEmittedError(RuntimeError):
    """The adapter can prove no PATCH request was emitted."""


class RoadmapWriteUncertainError(RuntimeError):
    """A PATCH may have been emitted but its outcome is not known."""


class RoadmapWriter(Protocol):
    def write(self, project: Project, body: str) -> None: ...


class IssueMappingReader(Protocol):
    def exists(self, repository_full_name: str, issue_number: int) -> bool: ...


@dataclass(frozen=True)
class CreateRoadmapChangeProposal:
    creation_command_id: UUID
    revision_command_id: UUID
    operations: Any
    created_by: str


@dataclass(frozen=True)
class CreateRoadmapChangeProposalRevision:
    revision_command_id: UUID
    expected_version: int
    operations: Any
    created_by: str


@dataclass(frozen=True)
class CancelRoadmapChangeProposal:
    cancellation_command_id: UUID
    expected_version: int
    cancelled_by: str


@dataclass(frozen=True)
class ConfirmRoadmapChangeProposal:
    revision: int
    preview_digest: str
    expected_proposal_version: int
    confirmation_command_id: UUID
    confirmed_by: str
    writeback_authorization_decision_id: UUID


@dataclass(frozen=True)
class ApplyRoadmapChangeProposal:
    application_command_id: UUID
    expected_proposal_version: int
    requested_by: str


@dataclass(frozen=True)
class ReconcileRoadmapChangeApplication:
    reconciliation_command_id: UUID
    expected_application_version: int
    reconciled_by: str


def _require_proposal(uow, identity):
    proposal = uow.roadmap_change_proposals.get(identity)
    if proposal is None:
        raise OrchestrationConflict("RoadmapChangeProposal not found")
    return proposal


def _require_application(uow, identity):
    application = uow.roadmap_change_applications.get(identity)
    if application is None:
        raise OrchestrationConflict("RoadmapChangeApplication not found")
    return application


def _require_admissible_decision(uow, decision_id):
    decision = uow.decisions.get(decision_id)
    if decision is None:
        raise OrchestrationConflict("Decision not found")
    if (
        decision.decision_type != DecisionType.SCOPE_DECISION.value
        or decision.effect is not DecisionEffect.HOLD_FOR_AUTHORIZATION
    ):
        raise OrchestrationConflict(
            "RoadmapChangeProposal requires an accepted SCOPE_DECISION held for authorization"
        )
    handoff = uow.handoffs.get(decision.source_handoff_id)
    if handoff is None or handoff.target_role != "PO":
        raise OrchestrationConflict("RoadmapChangeProposal requires Product Owner Decision provenance")
    return decision, handoff


def _require_writeback_authorization(uow, decision_id, project_id):
    decision = uow.decisions.get(decision_id)
    if decision is None:
        raise OrchestrationConflict(
            "Direct GitHub writeback requires an explicit Product Decision accepting the residual race"
        )
    handoff = uow.handoffs.get(decision.source_handoff_id)
    if (
        handoff is None
        or handoff.project_id != project_id
        or handoff.target_role != "PO"
        or handoff.purpose != HandoffPurpose.ROADMAP_REVIEW.value
        or decision.decision_type != DecisionType.SCOPE_DECISION.value
        or decision.effect is not DecisionEffect.CONTINUE_IN_SCOPE
    ):
        raise OrchestrationConflict(
            "Product Decision does not authorize direct GitHub writeback for this project"
        )
    return decision


def _project_for_target(project_catalog, proposal):
    project = project_catalog.get(proposal.project_id)
    if project is None:
        raise OrchestrationConflict("Project not found")
    if (
        project.repository_full_name != proposal.repository_full_name
        or project.roadmap_issue_number != proposal.roadmap_issue_number
    ):
        raise OrchestrationConflict(
            "Proposal GitHub target is frozen and no longer matches project configuration"
        )
    return project


def _read_target(project, roadmap_reader):
    try:
        return read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError as exc:
        raise OrchestrationConflict("Canonical roadmap unavailable") from exc


def _try_read_target(project, roadmap_reader):
    try:
        return read_project_roadmap(project, reader=roadmap_reader)
    except RoadmapSourceError:
        return None


def _new_revision(
    proposal,
    revision_number,
    command_id,
    operations,
    created_by,
    roadmap,
):
    normalized = canonical_operations(operations)
    proposed_body = generate_proposed_body(roadmap.issue.body, normalized)
    return RoadmapChangeProposalRevision(
        proposal_id=proposal.proposal_id,
        revision=revision_number,
        base_body=roadmap.issue.body,
        base_body_hash=body_hash(roadmap.issue.body),
        base_updated_at=roadmap.issue.updated_at,
        proposed_body=proposed_body,
        proposed_body_hash=body_hash(proposed_body),
        operations_json=operations_json(normalized),
        generator_version=GENERATOR_VERSION,
        validation_version=VALIDATION_VERSION,
        created_by=created_by,
        created_at=utc_now(),
        revision_command_id=command_id,
    )


def create_roadmap_change_proposal(
    decision_id,
    command: CreateRoadmapChangeProposal,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    if not command.created_by.strip():
        raise ValueError("created_by must not be blank")
    normalized = canonical_operations(command.operations)
    with uow_factory() as uow:
        existing = uow.roadmap_change_proposals.by_command(command.creation_command_id)
        if existing:
            revision = uow.roadmap_change_proposal_revisions.get(existing.proposal_id, 1)
            if (
                existing.source_decision_id != decision_id
                or existing.created_by != command.created_by
                or revision is None
                or revision.revision_command_id != command.revision_command_id
                or revision.operations != normalized
            ):
                raise OrchestrationConflict(
                    "Proposal creation command already used with different content"
                )
            return existing

        _, handoff = _require_admissible_decision(uow, decision_id)
        project = project_catalog.get(handoff.project_id)
        if project is None:
            raise OrchestrationConflict("Project not found")
        roadmap = _read_target(project, roadmap_reader)

        proposal = RoadmapChangeProposal(
            proposal_id=uuid4(),
            project_id=project.project_id,
            repository_full_name=project.repository_full_name,
            roadmap_issue_number=project.roadmap_issue_number,
            source_decision_id=decision_id,
            status=ProposalStatus.DRAFT,
            version=1,
            current_revision=1,
            creation_command_id=command.creation_command_id,
            created_by=command.created_by,
            created_at=utc_now(),
        )
        revision = _new_revision(
            proposal,
            1,
            command.revision_command_id,
            normalized,
            command.created_by,
            roadmap,
        )
        uow.roadmap_change_proposals.add(proposal)
        uow.flush()
        uow.roadmap_change_proposal_revisions.add(revision)
        uow.commit()
        return proposal


def create_roadmap_change_proposal_revision(
    proposal_id,
    command: CreateRoadmapChangeProposalRevision,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    if not command.created_by.strip():
        raise ValueError("created_by must not be blank")
    normalized = canonical_operations(command.operations)
    with uow_factory() as uow:
        replay = uow.roadmap_change_proposal_revisions.by_command(command.revision_command_id)
        if replay:
            if (
                replay.proposal_id != proposal_id
                or replay.created_by != command.created_by
                or replay.operations != normalized
            ):
                raise OrchestrationConflict(
                    "Revision command already used with different content"
                )
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Only a DRAFT proposal can be revised")
        if proposal.version != command.expected_version:
            raise OrchestrationConflict("Proposal version changed; refresh before revising")

        project = _project_for_target(project_catalog, proposal)
        roadmap = _read_target(project, roadmap_reader)
        next_revision = proposal.current_revision + 1
        revision = _new_revision(
            proposal,
            next_revision,
            command.revision_command_id,
            normalized,
            command.created_by,
            roadmap,
        )
        updated = replace(
            proposal,
            version=proposal.version + 1,
            current_revision=next_revision,
        )
        uow.roadmap_change_proposal_revisions.add(revision)
        uow.roadmap_change_proposals.save(updated, command.expected_version)
        uow.commit()
        return revision


def cancel_roadmap_change_proposal(
    proposal_id,
    command: CancelRoadmapChangeProposal,
    *,
    uow_factory,
):
    if not command.cancelled_by.strip():
        raise ValueError("cancelled_by must not be blank")
    with uow_factory() as uow:
        replay = uow.roadmap_change_proposals.by_cancellation_command(
            command.cancellation_command_id
        )
        if replay:
            if replay.proposal_id != proposal_id or replay.cancelled_by != command.cancelled_by:
                raise OrchestrationConflict(
                    "Cancellation command already used with different content"
                )
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Proposal is not cancellable")
        if proposal.version != command.expected_version:
            raise OrchestrationConflict("Proposal version changed; refresh before cancelling")
        cancelled = replace(
            proposal,
            status=ProposalStatus.CANCELLED,
            version=proposal.version + 1,
            cancelled_by=command.cancelled_by,
            cancelled_at=utc_now(),
            cancellation_command_id=command.cancellation_command_id,
        )
        uow.roadmap_change_proposals.save(cancelled, command.expected_version)
        uow.commit()
        return cancelled


def confirm_roadmap_change_proposal(
    proposal_id,
    command: ConfirmRoadmapChangeProposal,
    *,
    project_catalog,
    roadmap_reader: RoadmapIssueReader,
    uow_factory,
):
    if command.revision < 1:
        raise ValueError("revision must be >= 1")
    if not command.preview_digest.strip() or not command.confirmed_by.strip():
        raise ValueError("preview_digest and confirmed_by must not be blank")

    with uow_factory() as uow:
        replay = uow.roadmap_change_proposals.by_confirmation_command(
            command.confirmation_command_id
        )
        if replay is not None:
            if (
                replay.proposal_id != proposal_id
                or replay.confirmed_revision != command.revision
                or replay.confirmed_preview_digest != command.preview_digest
                or replay.confirmed_by != command.confirmed_by
                or replay.writeback_authorization_decision_id
                != command.writeback_authorization_decision_id
            ):
                raise OrchestrationConflict(
                    "Confirmation command already used with different content"
                )
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Only a DRAFT proposal can be confirmed")
        if proposal.version != command.expected_proposal_version:
            raise OrchestrationConflict("Proposal version changed; refresh before confirming")
        project = _project_for_target(project_catalog, proposal)
        _require_writeback_authorization(
            uow,
            command.writeback_authorization_decision_id,
            proposal.project_id,
        )
        revision = uow.roadmap_change_proposal_revisions.get(
            proposal.proposal_id,
            command.revision,
        )
        if revision is None:
            raise OrchestrationConflict("Proposal revision not found")
        preview = build_preview(proposal, revision)
        if preview["preview_digest"] != command.preview_digest:
            raise OrchestrationConflict("Preview digest does not match the exact proposal revision")
        if preview["blocking_diagnostics"]:
            raise OrchestrationConflict("Proposal revision has blocking diagnostics")

    remote = _read_target(project, roadmap_reader)
    if (
        remote.issue.body != revision.base_body
        or body_hash(remote.issue.body) != revision.base_body_hash
    ):
        raise OrchestrationConflict(
            "Canonical roadmap changed; create a new revision and preview before confirming"
        )

    with uow_factory() as uow:
        replay = uow.roadmap_change_proposals.by_confirmation_command(
            command.confirmation_command_id
        )
        if replay is not None:
            if (
                replay.proposal_id != proposal_id
                or replay.confirmed_revision != command.revision
                or replay.confirmed_preview_digest != command.preview_digest
                or replay.confirmed_by != command.confirmed_by
                or replay.writeback_authorization_decision_id
                != command.writeback_authorization_decision_id
            ):
                raise OrchestrationConflict(
                    "Confirmation command already used with different content"
                )
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.DRAFT:
            raise OrchestrationConflict("Proposal changed while confirmation was in progress")
        if proposal.version != command.expected_proposal_version:
            raise OrchestrationConflict("Proposal version changed; refresh before confirming")
        _project_for_target(project_catalog, proposal)
        _require_writeback_authorization(
            uow,
            command.writeback_authorization_decision_id,
            proposal.project_id,
        )
        revision = uow.roadmap_change_proposal_revisions.get(proposal_id, command.revision)
        if revision is None:
            raise OrchestrationConflict("Proposal revision not found")
        preview = build_preview(proposal, revision)
        if preview["preview_digest"] != command.preview_digest:
            raise OrchestrationConflict("Preview digest changed")
        confirmed = replace(
            proposal,
            status=ProposalStatus.CONFIRMED,
            version=proposal.version + 1,
            confirmed_revision=command.revision,
            confirmed_preview_digest=command.preview_digest,
            confirmation_command_id=command.confirmation_command_id,
            confirmed_by=command.confirmed_by,
            confirmed_at=utc_now(),
            writeback_authorization_decision_id=command.writeback_authorization_decision_id,
        )
        uow.roadmap_change_proposals.save(
            confirmed,
            command.expected_proposal_version,
        )
        uow.commit()
        return confirmed


def _required_issue_numbers(revision):
    base = parse_canonical_pipeline(revision.base_body)
    proposed = parse_canonical_pipeline(revision.proposed_body)
    if not base.valid or not proposed.valid:
        raise OrchestrationConflict("Proposal revision contains an invalid canonical pipeline")
    base_keys = {item.key for item in base.work_items}
    mappings = parse_issue_mappings(revision.proposed_body)
    result = []
    for item in proposed.work_items:
        if item.key in base_keys:
            continue
        issue_number = mappings.get(item.key)
        if issue_number is None:
            raise OrchestrationConflict(
                f"Replacement WorkItem {item.key} has no existing GitHub issue mapping"
            )
        result.append((item.key, issue_number))
    return result


def _validate_issue_mappings(revision, repository_full_name, issue_mapping_reader):
    for key, issue_number in _required_issue_numbers(revision):
        try:
            exists = issue_mapping_reader.exists(repository_full_name, issue_number)
        except Exception as exc:
            raise OrchestrationConflict(
                f"Unable to revalidate GitHub issue mapping for {key}"
            ) from exc
        if not exists:
            raise OrchestrationConflict(
                f"GitHub issue mapping for {key} does not exist; apply is blocked"
            )


def _application_payload(uow, application):
    attempts = uow.roadmap_change_application_attempts.list_for_application(
        application.application_id
    )
    return {
        **asdict(application),
        "attempts": [asdict(attempt) for attempt in attempts],
        "allowed_actions": (
            ["RECONCILE"]
            if application.status is ApplicationStatus.RECONCILIATION_REQUIRED
            else []
        ),
    }


def _set_application_state(
    uow,
    application,
    status,
    *,
    last_remote_body_hash=None,
    release_fence=False,
):
    updated = replace(
        application,
        status=status,
        version=application.version + 1,
        updated_at=utc_now(),
        last_remote_body_hash=last_remote_body_hash,
    )
    uow.roadmap_change_applications.save(updated, application.version)
    if release_fence:
        uow.roadmap_target_fences.release(
            application.repository_full_name,
            application.roadmap_issue_number,
            application.application_id,
        )
    return updated


def _complete_attempt(uow, application_id, outcome, *, detail=None):
    attempts = uow.roadmap_change_application_attempts.list_for_application(application_id)
    if not attempts:
        raise OrchestrationConflict("Roadmap application attempt not found")
    attempt = attempts[-1]
    completed = replace(
        attempt,
        outcome=outcome,
        completed_at=utc_now(),
        detail=detail,
    )
    uow.roadmap_change_application_attempts.save(completed)
    return completed


def _mark_terminal(
    application_id,
    status,
    *,
    remote_body,
    outcome,
    detail,
    proposal_applied,
    uow_factory,
):
    with uow_factory() as uow:
        application = _require_application(uow, application_id)
        remote_hash = body_hash(remote_body) if remote_body is not None else None
        release = status in {
            ApplicationStatus.APPLIED,
            ApplicationStatus.NOT_APPLIED,
            ApplicationStatus.CONFLICT,
        }
        updated = _set_application_state(
            uow,
            application,
            status,
            last_remote_body_hash=remote_hash,
            release_fence=release,
        )
        _complete_attempt(
            uow,
            application.application_id,
            outcome,
            detail=detail,
        )
        if proposal_applied:
            proposal = _require_proposal(uow, application.proposal_id)
            if proposal.status is ProposalStatus.CONFIRMED:
                proposal = replace(
                    proposal,
                    status=ProposalStatus.APPLIED,
                    version=proposal.version + 1,
                    applied_at=utc_now(),
                )
                uow.roadmap_change_proposals.save(proposal, proposal.version - 1)
        uow.commit()
        return updated


def apply_roadmap_change_proposal(
    proposal_id,
    command: ApplyRoadmapChangeProposal,
    *,
    project_catalog,
    roadmap_reader,
    roadmap_writer: RoadmapWriter,
    issue_mapping_reader: IssueMappingReader,
    uow_factory,
):
    if not command.requested_by.strip():
        raise ValueError("requested_by must not be blank")

    with uow_factory() as uow:
        replay = uow.roadmap_change_applications.by_command(command.application_command_id)
        if replay is not None:
            if (
                replay.proposal_id != proposal_id
                or replay.requested_by != command.requested_by
            ):
                raise OrchestrationConflict(
                    "Application command already used with different content"
                )
            return replay

        proposal = _require_proposal(uow, proposal_id)
        if proposal.status is not ProposalStatus.CONFIRMED:
            raise OrchestrationConflict("Only a CONFIRMED proposal can be applied")
        if proposal.version != command.expected_proposal_version:
            raise OrchestrationConflict("Proposal version changed; refresh before applying")
        if proposal.confirmed_revision is None:
            raise OrchestrationConflict("Confirmed proposal has no exact revision")
        project = _project_for_target(project_catalog, proposal)
        _require_writeback_authorization(
            uow,
            proposal.writeback_authorization_decision_id,
            proposal.project_id,
        )
        revision = uow.roadmap_change_proposal_revisions.get(
            proposal_id,
            proposal.confirmed_revision,
        )
        if revision is None:
            raise OrchestrationConflict("Confirmed proposal revision not found")

    _validate_issue_mappings(
        revision,
        proposal.repository_full_name,
        issue_mapping_reader,
    )

    with uow_factory() as uow:
        replay = uow.roadmap_change_applications.by_command(command.application_command_id)
        if replay is not None:
            if (
                replay.proposal_id != proposal_id
                or replay.requested_by != command.requested_by
            ):
                raise OrchestrationConflict(
                    "Application command already used with different content"
                )
            return replay
        proposal = _require_proposal(uow, proposal_id)
        if (
            proposal.status is not ProposalStatus.CONFIRMED
            or proposal.version != command.expected_proposal_version
        ):
            raise OrchestrationConflict("Proposal changed while apply was being prepared")
        project = _project_for_target(project_catalog, proposal)
        _require_writeback_authorization(
            uow,
            proposal.writeback_authorization_decision_id,
            proposal.project_id,
        )
        revision = uow.roadmap_change_proposal_revisions.get(
            proposal_id,
            proposal.confirmed_revision,
        )
        now = utc_now()
        application = RoadmapChangeApplication(
            application_id=uuid4(),
            proposal_id=proposal.proposal_id,
            revision=revision.revision,
            project_id=proposal.project_id,
            repository_full_name=proposal.repository_full_name,
            roadmap_issue_number=proposal.roadmap_issue_number,
            base_body=revision.base_body,
            base_body_hash=revision.base_body_hash,
            expected_body=revision.proposed_body,
            expected_body_hash=revision.proposed_body_hash,
            application_command_id=command.application_command_id,
            requested_by=command.requested_by,
            status=ApplicationStatus.PREPARED,
            version=1,
            created_at=now,
            updated_at=now,
        )
        attempt = RoadmapChangeApplicationAttempt(
            attempt_id=uuid4(),
            application_id=application.application_id,
            attempt_number=1,
            command_id=command.application_command_id,
            outcome=ApplicationAttemptOutcome.PREPARED,
            patch_may_have_been_emitted=False,
            started_at=now,
        )
        uow.roadmap_change_applications.add(application)
        uow.flush()
        uow.roadmap_target_fences.claim(
            application.repository_full_name,
            application.roadmap_issue_number,
            application.application_id,
        )
        uow.roadmap_change_application_attempts.add(attempt)
        uow.commit()

    remote = _try_read_target(project, roadmap_reader)
    if remote is None:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.NOT_APPLIED,
            remote_body=None,
            outcome=ApplicationAttemptOutcome.NOT_EMITTED,
            detail="GitHub reread failed before PATCH; no PATCH was emitted.",
            proposal_applied=False,
            uow_factory=uow_factory,
        )
    if remote.issue.body != application.base_body:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.CONFLICT,
            remote_body=remote.issue.body,
            outcome=ApplicationAttemptOutcome.CONFLICT,
            detail="Remote roadmap changed before PATCH.",
            proposal_applied=False,
            uow_factory=uow_factory,
        )

    with uow_factory() as uow:
        current = _require_application(uow, application.application_id)
        if current.status is not ApplicationStatus.PREPARED:
            raise OrchestrationConflict("Roadmap application changed before PATCH")
        applying = _set_application_state(
            uow,
            current,
            ApplicationStatus.APPLYING,
            last_remote_body_hash=body_hash(remote.issue.body),
        )
        attempts = uow.roadmap_change_application_attempts.list_for_application(
            applying.application_id
        )
        attempt = attempts[-1]
        uow.roadmap_change_application_attempts.save(
            replace(
                attempt,
                outcome=ApplicationAttemptOutcome.APPLYING,
                patch_may_have_been_emitted=True,
            )
        )
        uow.commit()
        application = applying

    try:
        roadmap_writer.write(project, application.expected_body)
    except RoadmapWriteNotEmittedError as exc:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.NOT_APPLIED,
            remote_body=application.base_body,
            outcome=ApplicationAttemptOutcome.NOT_EMITTED,
            detail=str(exc),
            proposal_applied=False,
            uow_factory=uow_factory,
        )
    except RoadmapWriteUncertainError as exc:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.RECONCILIATION_REQUIRED,
            remote_body=None,
            outcome=ApplicationAttemptOutcome.RECONCILIATION_REQUIRED,
            detail=str(exc),
            proposal_applied=False,
            uow_factory=uow_factory,
        )

    remote_after = _try_read_target(project, roadmap_reader)
    if remote_after is None:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.RECONCILIATION_REQUIRED,
            remote_body=None,
            outcome=ApplicationAttemptOutcome.RECONCILIATION_REQUIRED,
            detail="GitHub unavailable after PATCH attempt.",
            proposal_applied=False,
            uow_factory=uow_factory,
        )
    if remote_after.issue.body == application.expected_body:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.APPLIED,
            remote_body=remote_after.issue.body,
            outcome=ApplicationAttemptOutcome.APPLIED,
            detail="Remote roadmap matches the confirmed proposed body.",
            proposal_applied=True,
            uow_factory=uow_factory,
        )
    if remote_after.issue.body != application.base_body:
        return _mark_terminal(
            application.application_id,
            ApplicationStatus.CONFLICT,
            remote_body=remote_after.issue.body,
            outcome=ApplicationAttemptOutcome.CONFLICT,
            detail="Remote roadmap differs from both base and expected body.",
            proposal_applied=False,
            uow_factory=uow_factory,
        )
    return _mark_terminal(
        application.application_id,
        ApplicationStatus.RECONCILIATION_REQUIRED,
        remote_body=remote_after.issue.body,
        outcome=ApplicationAttemptOutcome.RECONCILIATION_REQUIRED,
        detail="Remote still equals base after a PATCH may have been emitted.",
        proposal_applied=False,
        uow_factory=uow_factory,
    )


def reconcile_roadmap_change_application(
    application_id,
    command: ReconcileRoadmapChangeApplication,
    *,
    project_catalog,
    roadmap_reader,
    uow_factory,
):
    if not command.reconciled_by.strip():
        raise ValueError("reconciled_by must not be blank")

    with uow_factory() as uow:
        replay = uow.roadmap_change_application_attempts.by_command(
            command.reconciliation_command_id
        )
        if replay is not None:
            if replay.application_id != application_id:
                raise OrchestrationConflict(
                    "Reconciliation command already used for another application"
                )
            return _require_application(uow, application_id)

        application = _require_application(uow, application_id)
        if application.version != command.expected_application_version:
            raise OrchestrationConflict(
                "Roadmap application version changed; refresh before reconciling"
            )
        if application.status not in {
            ApplicationStatus.PREPARED,
            ApplicationStatus.APPLYING,
            ApplicationStatus.RECONCILIATION_REQUIRED,
        }:
            raise OrchestrationConflict("Roadmap application is not reconcilable")
        proposal = _require_proposal(uow, application.proposal_id)
        project = _project_for_target(project_catalog, proposal)
        existing_attempts = uow.roadmap_change_application_attempts.list_for_application(
            application_id
        )
        patch_may_have_been_emitted = any(
            attempt.patch_may_have_been_emitted for attempt in existing_attempts
        )

    remote = _try_read_target(project, roadmap_reader)

    with uow_factory() as uow:
        replay = uow.roadmap_change_application_attempts.by_command(
            command.reconciliation_command_id
        )
        if replay is not None:
            if replay.application_id != application_id:
                raise OrchestrationConflict(
                    "Reconciliation command already used for another application"
                )
            return _require_application(uow, application_id)
        application = _require_application(uow, application_id)
        if application.version != command.expected_application_version:
            raise OrchestrationConflict(
                "Roadmap application changed while reconciliation was in progress"
            )
        attempts = uow.roadmap_change_application_attempts.list_for_application(application_id)
        started = utc_now()
        attempt = RoadmapChangeApplicationAttempt(
            attempt_id=uuid4(),
            application_id=application.application_id,
            attempt_number=len(attempts) + 1,
            command_id=command.reconciliation_command_id,
            outcome=ApplicationAttemptOutcome.RECONCILIATION_REQUIRED,
            patch_may_have_been_emitted=patch_may_have_been_emitted,
            started_at=started,
            completed_at=started,
            detail="Explicit reconciliation.",
        )

        if remote is None:
            status = ApplicationStatus.RECONCILIATION_REQUIRED
            outcome = ApplicationAttemptOutcome.RECONCILIATION_REQUIRED
            remote_hash = None
            release = False
        elif remote.issue.body == application.expected_body:
            status = ApplicationStatus.APPLIED
            outcome = ApplicationAttemptOutcome.APPLIED
            remote_hash = body_hash(remote.issue.body)
            release = True
        elif remote.issue.body != application.base_body:
            status = ApplicationStatus.CONFLICT
            outcome = ApplicationAttemptOutcome.CONFLICT
            remote_hash = body_hash(remote.issue.body)
            release = True
        elif patch_may_have_been_emitted:
            status = ApplicationStatus.RECONCILIATION_REQUIRED
            outcome = ApplicationAttemptOutcome.RECONCILIATION_REQUIRED
            remote_hash = body_hash(remote.issue.body)
            release = False
        else:
            status = ApplicationStatus.NOT_APPLIED
            outcome = ApplicationAttemptOutcome.NOT_EMITTED
            remote_hash = body_hash(remote.issue.body)
            release = True

        attempt = replace(attempt, outcome=outcome)
        uow.roadmap_change_application_attempts.add(attempt)
        updated = _set_application_state(
            uow,
            application,
            status,
            last_remote_body_hash=remote_hash,
            release_fence=release,
        )
        if status is ApplicationStatus.APPLIED:
            proposal = _require_proposal(uow, application.proposal_id)
            if proposal.status is ProposalStatus.CONFIRMED:
                applied = replace(
                    proposal,
                    status=ProposalStatus.APPLIED,
                    version=proposal.version + 1,
                    applied_at=utc_now(),
                )
                uow.roadmap_change_proposals.save(applied, proposal.version)
        uow.commit()
        return updated


def read_roadmap_change_proposal(proposal_id, *, uow_factory):
    with uow_factory() as uow:
        proposal = _require_proposal(uow, proposal_id)
        revisions = uow.roadmap_change_proposal_revisions.list_for_proposal(proposal_id)
        applications = uow.roadmap_change_applications.list_for_proposal(proposal_id)
        if proposal.status is ProposalStatus.DRAFT:
            actions = ["CREATE_REVISION", "PREVIEW", "CANCEL", "CONFIRM"]
        elif proposal.status is ProposalStatus.CONFIRMED:
            actions = ["PREVIEW", "APPLY"]
        else:
            actions = ["PREVIEW"]
        return {
            **asdict(proposal),
            "revisions": [
                {
                    "revision": revision.revision,
                    "base_body_hash": revision.base_body_hash,
                    "base_updated_at": revision.base_updated_at,
                    "proposed_body_hash": revision.proposed_body_hash,
                    "operations": list(revision.operations),
                    "generator_version": revision.generator_version,
                    "validation_version": revision.validation_version,
                    "created_by": revision.created_by,
                    "created_at": revision.created_at,
                    "revision_command_id": revision.revision_command_id,
                }
                for revision in revisions
            ],
            "applications": [
                _application_payload(uow, application)
                for application in applications
            ],
            "allowed_actions": actions,
            "github_writeback_available": proposal.status
            in {ProposalStatus.CONFIRMED, ProposalStatus.APPLIED},
            "github_writeback_note": (
                "Direct writeback requires an explicit PO SCOPE_DECISION with "
                "CONTINUE_IN_SCOPE accepting the residual GET-to-PATCH race."
            ),
        }


def read_roadmap_change_application(application_id, *, uow_factory):
    with uow_factory() as uow:
        application = _require_application(uow, application_id)
        return _application_payload(uow, application)


def preview_roadmap_change_proposal_revision(
    proposal_id,
    revision_number,
    *,
    uow_factory,
):
    with uow_factory() as uow:
        proposal = _require_proposal(uow, proposal_id)
        revision = uow.roadmap_change_proposal_revisions.get(proposal_id, revision_number)
        if revision is None:
            raise OrchestrationConflict("Proposal revision not found")
        return build_preview(proposal, revision)
