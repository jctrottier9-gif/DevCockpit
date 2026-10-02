from dataclasses import replace
from uuid import uuid4

import pytest

from app.application.chatgpt_responses import ImportChatGptResponseCommand, import_chatgpt_response
from app.application.handoffs import AcceptDecision, CreateHandoff, accept_decision, create_handoff
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.application.roadmap_changes import (
    CancelRoadmapChangeProposal,
    CreateRoadmapChangeProposal,
    CreateRoadmapChangeProposalRevision,
    cancel_roadmap_change_proposal,
    create_roadmap_change_proposal,
    create_roadmap_change_proposal_revision,
    preview_roadmap_change_proposal_revision,
    read_roadmap_change_proposal,
)
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.handoff import DecisionEffect, OrchestrationConflict
from app.domain.project import Project
from app.domain.roadmap_change import ProposalStatus
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
KEY = "DC-041A"


class Roadmap:
    body = """# Roadmap

## État courant

État A

## Issues de livraison

| Clé | Issue |
|---|---:|
| DC-041A | #27 |

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
ASTRA-041 | ARCHITECTURE_GATE | DONE | #1 | MAIN | gate
DC-041A | WORK | READY | #11 | MAIN | local PO loop
DC-041B | WORK | BLOCKED | #11 | MAIN | writeback
<!-- /COCKPIT_PIPELINE_V1 -->
"""
    updated_at = "2026-10-01T12:00:00Z"

    def read(self, project):
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            self.updated_at,
        )


class Evidence:
    def read(self, project, item):
        return ExecutionEvidence(default_branch="main")


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/proposal.db", execution_poll_seconds=0)
    upgrade_database(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    uow = lambda: SqlAlchemyUnitOfWork(factory)
    source = create_prompt_dispatch(
        CreatePromptDispatchCommand("DevCockpit", KEY, "DEV", "DEV source", str(uuid4())),
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


def response(env, dispatch_id, text="PO response"):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=env["uow"])
    delivery = next(item for item in outbound if item.dispatch_id == dispatch_id)
    command = ImportChatGptResponseCommand(uuid4(), delivery.delivery_id, delivery.session, text)
    import_chatgpt_response(command, uow_factory=env["uow"])
    return command


def accepted_po_decision(env, *, purpose="ROADMAP_REVIEW", decision_type="SCOPE_DECISION"):
    handoff = create_handoff(
        PROJECT,
        KEY,
        CreateHandoff(
            uuid4(),
            env["source"].dispatch_id,
            "Décider le scope",
            "Contexte",
            "JC",
            target_role="PO",
            purpose=purpose,
        ),
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    returned = response(env, handoff.request_dispatch_id)
    accept_decision(
        handoff.handoff_id,
        AcceptDecision(
            uuid4(),
            handoff.version,
            returned.response_id,
            "Un changement de roadmap est proposé.",
            DecisionEffect.HOLD_FOR_AUTHORIZATION,
            "JC",
            decision_type,
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )
    with env["uow"]() as uow:
        return uow.decisions.for_handoff(handoff.handoff_id)


def operations(replacement="État B"):
    return [{
        "type": "update_current_state_prose",
        "expected": "État A",
        "replacement": replacement,
    }]


def test_proposal_requires_admissible_decision_and_raw_response_is_insufficient(env):
    with pytest.raises(OrchestrationConflict, match="Decision not found"):
        create_roadmap_change_proposal(
            uuid4(),
            CreateRoadmapChangeProposal(uuid4(), uuid4(), operations(), "JC"),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    clarification = accepted_po_decision(
        env,
        purpose="PRODUCT_CLARIFICATION",
        decision_type="PRODUCT_CLARIFICATION",
    )
    with pytest.raises(OrchestrationConflict, match="SCOPE_DECISION"):
        create_roadmap_change_proposal(
            clarification.decision_id,
            CreateRoadmapChangeProposal(uuid4(), uuid4(), operations(), "JC"),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )


def test_proposal_creation_revision_preview_cancellation_and_restart(env):
    decision = accepted_po_decision(env)
    command = CreateRoadmapChangeProposal(uuid4(), uuid4(), operations(), "JC")
    proposal = create_roadmap_change_proposal(
        decision.decision_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    replay = create_roadmap_change_proposal(
        decision.decision_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert replay == proposal
    assert proposal.repository_full_name == PROJECT.repository_full_name
    assert proposal.roadmap_issue_number == 1
    assert proposal.status is ProposalStatus.DRAFT

    with pytest.raises(OrchestrationConflict, match="different content"):
        create_roadmap_change_proposal(
            decision.decision_id,
            replace(command, operations=operations("État C")),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    changed_catalog = ProjectCatalog((
        Project("DevCockpit", "jctrottier9-gif/AnotherRepo", 99),
    ))
    with pytest.raises(OrchestrationConflict, match="target is frozen"):
        create_roadmap_change_proposal_revision(
            proposal.proposal_id,
            CreateRoadmapChangeProposalRevision(
                uuid4(),
                proposal.version,
                operations("État C"),
                "JC",
            ),
            project_catalog=changed_catalog,
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    preview1 = preview_roadmap_change_proposal_revision(
        proposal.proposal_id,
        1,
        uow_factory=env["uow"],
    )
    assert preview1["base_body"] == env["roadmap"].body
    assert "État B" in preview1["proposed_body"]
    assert "CONFIRM" not in preview1["allowed_actions"]
    assert "APPLY" not in preview1["allowed_actions"]
    assert preview_roadmap_change_proposal_revision(
        proposal.proposal_id,
        1,
        uow_factory=env["uow"],
    ) == preview1

    revision2_command = CreateRoadmapChangeProposalRevision(
        uuid4(),
        proposal.version,
        operations("État C"),
        "JC",
    )
    revision2 = create_roadmap_change_proposal_revision(
        proposal.proposal_id,
        revision2_command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert revision2.revision == 2
    assert "État C" in revision2.proposed_body
    assert create_roadmap_change_proposal_revision(
        proposal.proposal_id,
        revision2_command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    ) == revision2

    preview2 = preview_roadmap_change_proposal_revision(
        proposal.proposal_id,
        2,
        uow_factory=env["uow"],
    )
    assert preview2["preview_digest"] != preview1["preview_digest"]
    assert "État B" in preview1["proposed_body"]

    current = read_roadmap_change_proposal(proposal.proposal_id, uow_factory=env["uow"])
    assert current["current_revision"] == 2
    assert [item["revision"] for item in current["revisions"]] == [1, 2]
    assert current["github_writeback_available"] is False

    cancel = CancelRoadmapChangeProposal(uuid4(), 2, "JC")
    cancelled = cancel_roadmap_change_proposal(
        proposal.proposal_id,
        cancel,
        uow_factory=env["uow"],
    )
    assert cancelled.status is ProposalStatus.CANCELLED
    assert cancel_roadmap_change_proposal(
        proposal.proposal_id,
        cancel,
        uow_factory=env["uow"],
    ) == cancelled
    with pytest.raises(OrchestrationConflict, match="cannot be revised"):
        create_roadmap_change_proposal_revision(
            proposal.proposal_id,
            CreateRoadmapChangeProposalRevision(uuid4(), cancelled.version, operations(), "JC"),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    # Re-open from a new Engine/UoW to prove persistence survives restart.
    restarted_engine = build_engine(env["settings"])
    restarted_factory = build_session_factory(restarted_engine)
    restarted_uow = lambda: SqlAlchemyUnitOfWork(restarted_factory)
    persisted = read_roadmap_change_proposal(
        proposal.proposal_id,
        uow_factory=restarted_uow,
    )
    assert persisted["status"] == ProposalStatus.CANCELLED
    assert persisted["current_revision"] == 2
    restarted_engine.dispose()


def test_revision_command_conflict_does_not_overwrite_revision_one(env):
    decision = accepted_po_decision(env)
    proposal = create_roadmap_change_proposal(
        decision.decision_id,
        CreateRoadmapChangeProposal(uuid4(), uuid4(), operations(), "JC"),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    command_id = uuid4()
    command = CreateRoadmapChangeProposalRevision(
        command_id,
        proposal.version,
        operations("État C"),
        "JC",
    )
    revision2 = create_roadmap_change_proposal_revision(
        proposal.proposal_id,
        command,
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    with pytest.raises(OrchestrationConflict, match="different content"):
        create_roadmap_change_proposal_revision(
            proposal.proposal_id,
            replace(command, operations=operations("État D")),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )
    with env["uow"]() as uow:
        revision1 = uow.roadmap_change_proposal_revisions.get(proposal.proposal_id, 1)
        assert "État B" in revision1.proposed_body
        assert "État C" in revision2.proposed_body
