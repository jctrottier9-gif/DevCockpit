from dataclasses import replace
from uuid import uuid4

import pytest

from app.application.chatgpt_responses import ImportChatGptResponseCommand, import_chatgpt_response
from app.application.handoffs import AcceptDecision, CreateHandoff, accept_decision, create_handoff
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.application.roadmap_changes import (
    ApplyRoadmapChangeProposal,
    ConfirmRoadmapChangeProposal,
    CreateRoadmapChangeProposal,
    ReconcileRoadmapChangeApplication,
    RoadmapWriteNotEmittedError,
    RoadmapWriteUncertainError,
    apply_roadmap_change_proposal,
    confirm_roadmap_change_proposal,
    create_roadmap_change_proposal,
    preview_roadmap_change_proposal_revision,
    read_roadmap_change_proposal,
    reconcile_roadmap_change_application,
)
from app.application.roadmaps import RoadmapIssue, RoadmapSourceError
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.handoff import DecisionEffect, OrchestrationConflict
from app.domain.project import Project
from app.domain.roadmap_change import ApplicationStatus, ProposalStatus
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
KEY = "DC-041B"
BASE = """# Roadmap

## État courant

État A

## Issues de livraison

| Clé | Issue |
|---|---:|
| DC-041B | #28 |

<!-- COCKPIT_PIPELINE_V2 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
DC-041 | WORK | SUPERSEDED | #11 | MAIN | parent | -
DC-041A | WORK | DONE | #27 | MAIN | A | DC-041
DC-041B | WORK | READY | #28 | MAIN | B | DC-041
<!-- /COCKPIT_PIPELINE_V2 -->
"""


class Roadmap:
    def __init__(self):
        self.body = BASE
        self.updated_at = "2026-10-02T01:00:00Z"
        self.offline = False

    def read(self, project):
        if self.offline:
            raise RoadmapSourceError("offline")
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            self.updated_at,
        )


class Evidence:
    def read(self, project, item):
        return ExecutionEvidence(default_branch="main")


class IssueMappings:
    def __init__(self, *, exists=True):
        self.available = exists
        self.calls = []

    def exists(self, repository_full_name, issue_number):
        self.calls.append((repository_full_name, issue_number))
        return self.available


class Writer:
    def __init__(self, roadmap, mode="apply"):
        self.roadmap = roadmap
        self.mode = mode
        self.calls = []

    def write(self, project, body):
        self.calls.append((project.repository_full_name, project.roadmap_issue_number, body))
        if self.mode == "not-emitted":
            raise RoadmapWriteNotEmittedError("request rejected before emission")
        if self.mode == "uncertain":
            raise RoadmapWriteUncertainError("connection lost after send")
        if self.mode == "divergent":
            self.roadmap.body = "external divergent body"
            return
        self.roadmap.body = body


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/writeback.db",
        execution_poll_seconds=0,
    )
    upgrade_database(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    uow = lambda: SqlAlchemyUnitOfWork(factory)
    source = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            "DevCockpit",
            KEY,
            "DEV",
            "DEV source",
            str(uuid4()),
        ),
        uow_factory=uow,
    )
    result = {
        "settings": settings,
        "engine": engine,
        "uow": uow,
        "roadmap": Roadmap(),
        "evidence": Evidence(),
        "catalog": ProjectCatalog((PROJECT,)),
        "source": source,
    }
    yield result
    engine.dispose()


def _returned_response(env, dispatch_id):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=env["uow"])
    delivery = next(item for item in outbound if item.dispatch_id == dispatch_id)
    command = ImportChatGptResponseCommand(
        uuid4(),
        delivery.delivery_id,
        delivery.session,
        "PO accepted scope decision",
    )
    import_chatgpt_response(command, uow_factory=env["uow"])
    return command


def _scope_decision(env, *, authorize_writeback):
    handoff = create_handoff(
        PROJECT,
        KEY,
        CreateHandoff(
            uuid4(),
            env["source"].dispatch_id,
            "Décider le changement de roadmap",
            "Contexte produit",
            "JC",
            target_role="PO",
            purpose="ROADMAP_REVIEW",
        ),
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    returned = _returned_response(env, handoff.request_dispatch_id)
    accept_decision(
        handoff.handoff_id,
        AcceptDecision(
            acceptance_command_id=uuid4(),
            expected_version=handoff.version,
            source_response_id=returned.response_id,
            summary="Le redécoupage est accepté.",
            effect=DecisionEffect.HOLD_FOR_AUTHORIZATION,
            accepted_by="JC",
            decision_type="SCOPE_DECISION",
            accepts_residual_writeback_risk=authorize_writeback,
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )
    with env["uow"]() as uow:
        return uow.decisions.for_handoff(handoff.handoff_id)


def _proposal(env, *, authorized=True):
    decision = _scope_decision(env, authorize_writeback=authorized)
    proposal = create_roadmap_change_proposal(
        decision.decision_id,
        CreateRoadmapChangeProposal(
            uuid4(),
            uuid4(),
            [{
                "type": "update_current_state_prose",
                "expected": "État A",
                "replacement": "État B",
            }],
            "JC",
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    preview = preview_roadmap_change_proposal_revision(
        proposal.proposal_id,
        1,
        uow_factory=env["uow"],
    )
    return proposal, preview


def _confirm(env, proposal, preview, *, command_id=None):
    command = ConfirmRoadmapChangeProposal(
        revision=1,
        preview_digest=preview["preview_digest"],
        expected_proposal_version=proposal.version,
        confirmation_command_id=command_id or uuid4(),
        confirmed_by="JC",
    )
    return confirm_roadmap_change_proposal(
        proposal.proposal_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    ), command


def test_confirmation_exact_revision_digest_stale_version_and_gate(env):
    unauthorized, unauthorized_preview = _proposal(env, authorized=False)
    with pytest.raises(OrchestrationConflict, match="explicit Product Decision"):
        _confirm(env, unauthorized, unauthorized_preview)

    # Use a fresh environment state for an authorized proposal by cancelling the
    # blocking PO Handoff created above would be artificial; the individual
    # cases below each use the fixture in separate tests.


def test_confirmation_is_exact_stale_safe_and_idempotent(env):
    proposal, preview = _proposal(env)

    with pytest.raises(OrchestrationConflict, match="Preview digest"):
        confirm_roadmap_change_proposal(
            proposal.proposal_id,
            ConfirmRoadmapChangeProposal(
                1,
                "0" * 64,
                proposal.version,
                uuid4(),
                "JC",
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    with pytest.raises(OrchestrationConflict, match="revision not found"):
        confirm_roadmap_change_proposal(
            proposal.proposal_id,
            ConfirmRoadmapChangeProposal(
                99,
                preview["preview_digest"],
                proposal.version,
                uuid4(),
                "JC",
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    with pytest.raises(OrchestrationConflict, match="version changed"):
        confirm_roadmap_change_proposal(
            proposal.proposal_id,
            ConfirmRoadmapChangeProposal(
                1,
                preview["preview_digest"],
                proposal.version + 1,
                uuid4(),
                "JC",
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    command_id = uuid4()
    confirmed, command = _confirm(env, proposal, preview, command_id=command_id)
    assert confirmed.status is ProposalStatus.CONFIRMED
    assert confirmed.confirmed_revision == 1
    assert confirmed.confirmed_preview_digest == preview["preview_digest"]

    replay = confirm_roadmap_change_proposal(
        proposal.proposal_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert replay == confirmed

    with pytest.raises(OrchestrationConflict, match="different content"):
        confirm_roadmap_change_proposal(
            proposal.proposal_id,
            replace(command, confirmed_by="Other"),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )


def test_confirmation_refuses_stale_remote_base(env):
    proposal, preview = _proposal(env)
    env["roadmap"].body = BASE.replace("État A", "external edit")

    with pytest.raises(OrchestrationConflict, match="Canonical roadmap changed"):
        _confirm(env, proposal, preview)


def test_apply_uses_exact_confirmed_body_and_is_restart_idempotent(env):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    writer = Writer(env["roadmap"])
    command = ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC")

    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )
    assert application.status is ApplicationStatus.APPLIED
    assert writer.calls == [
        (PROJECT.repository_full_name, PROJECT.roadmap_issue_number, preview["proposed_body"])
    ]

    restarted_engine = build_engine(env["settings"])
    restarted_factory = build_session_factory(restarted_engine)
    restarted_uow = lambda: SqlAlchemyUnitOfWork(restarted_factory)
    replay = apply_roadmap_change_proposal(
        proposal.proposal_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=restarted_uow,
    )
    assert replay.application_id == application.application_id
    assert len(writer.calls) == 1
    persisted = read_roadmap_change_proposal(
        proposal.proposal_id,
        uow_factory=restarted_uow,
    )
    assert persisted["status"] == ProposalStatus.APPLIED
    restarted_engine.dispose()


def test_apply_refuses_stale_remote_before_patch(env):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    env["roadmap"].body = BASE.replace("État A", "external edit")
    writer = Writer(env["roadmap"])

    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )
    assert application.status is ApplicationStatus.CONFLICT
    assert writer.calls == []


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("not-emitted", ApplicationStatus.NOT_APPLIED),
        ("uncertain", ApplicationStatus.RECONCILIATION_REQUIRED),
        ("divergent", ApplicationStatus.CONFLICT),
    ],
)
def test_apply_represents_remote_uncertainty_without_retry(env, mode, expected):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    writer = Writer(env["roadmap"], mode)

    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )
    assert application.status is expected
    assert len(writer.calls) == 1


def test_explicit_reconciliation_never_patches_and_classifies_remote_state(env):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    writer = Writer(env["roadmap"], "uncertain")
    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )
    assert application.status is ApplicationStatus.RECONCILIATION_REQUIRED
    assert len(writer.calls) == 1

    env["roadmap"].body = preview["proposed_body"]
    command = ReconcileRoadmapChangeApplication(
        uuid4(),
        application.version,
        "JC",
    )
    reconciled = reconcile_roadmap_change_application(
        application.application_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert reconciled.status is ApplicationStatus.APPLIED
    assert len(writer.calls) == 1

    replay = reconcile_roadmap_change_application(
        application.application_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert replay.status is ApplicationStatus.APPLIED
    assert len(writer.calls) == 1


def test_remote_base_after_potential_patch_stays_reconciliation_required(env):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    writer = Writer(env["roadmap"], "uncertain")
    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )

    reconciled = reconcile_roadmap_change_application(
        application.application_id,
        ReconcileRoadmapChangeApplication(uuid4(), application.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert reconciled.status is ApplicationStatus.RECONCILIATION_REQUIRED


def test_github_unavailable_after_uncertain_patch_remains_reconciliation_required(env):
    proposal, preview = _proposal(env)
    confirmed, _ = _confirm(env, proposal, preview)
    writer = Writer(env["roadmap"], "uncertain")
    application = apply_roadmap_change_proposal(
        proposal.proposal_id,
        ApplyRoadmapChangeProposal(uuid4(), confirmed.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        roadmap_writer=writer,
        issue_mapping_reader=IssueMappings(),
        uow_factory=env["uow"],
    )
    env["roadmap"].offline = True

    reconciled = reconcile_roadmap_change_application(
        application.application_id,
        ReconcileRoadmapChangeApplication(uuid4(), application.version, "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert reconciled.status is ApplicationStatus.RECONCILIATION_REQUIRED
