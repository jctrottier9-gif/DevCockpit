from uuid import uuid4

import pytest

from app.application.chatgpt_responses import ImportChatGptResponseCommand, import_chatgpt_response
from app.application.handoffs import (
    AcceptDecision,
    CreateHandoff,
    TransferHandoffToPO,
    accept_decision,
    create_handoff,
    read_orchestration,
    transfer_handoff_to_po,
)
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.execution import ExecutionEvidence
from app.domain.handoff import DecisionEffect, HandoffStatus, OrchestrationConflict
from app.domain.project import Project
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
KEY = "DC-041A"


class Roadmap:
    def read(self, project):
        return RoadmapIssue(
            project.repository_full_name,
            1,
            f"""# Roadmap
<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
ASTRA-041 | ARCHITECTURE_GATE | DONE | #1 | MAIN | gate
{KEY} | WORK | READY | #11 | MAIN | PO loop
DC-041B | WORK | BLOCKED | #11 | MAIN | writeback
<!-- /COCKPIT_PIPELINE_V1 -->""",
            "2026-10-01T12:00:00Z",
        )


class Evidence:
    def read(self, project, item):
        return ExecutionEvidence(default_branch="main")


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/po.db",
        execution_poll_seconds=0,
    )
    upgrade_database(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    uow = lambda: SqlAlchemyUnitOfWork(factory)
    source = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            PROJECT.project_id,
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
        "source": source,
        "roadmap": Roadmap(),
        "evidence": Evidence(),
        "catalog": ProjectCatalog((PROJECT,)),
    }
    yield result
    engine.dispose()


def returned_response(env, dispatch_id, text="Réponse"):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=env["uow"])
    delivery = next(item for item in outbound if item.dispatch_id == dispatch_id)
    command = ImportChatGptResponseCommand(
        uuid4(),
        delivery.delivery_id,
        delivery.session,
        text,
    )
    import_chatgpt_response(command, uow_factory=env["uow"])
    return command


def direct_handoff(env, role="ARCH", purpose="TECHNICAL_GUIDANCE"):
    return create_handoff(
        PROJECT,
        KEY,
        CreateHandoff(
            uuid4(),
            env["source"].dispatch_id,
            "Question",
            "Contexte",
            "JC",
            target_role=role,
            purpose=purpose,
        ),
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )


def accept(env, handoff, response_id, *, decision_type, effect=DecisionEffect.CONTINUE_IN_SCOPE):
    return accept_decision(
        handoff.handoff_id,
        AcceptDecision(
            uuid4(),
            handoff.version,
            response_id,
            "Conclusion acceptée",
            effect,
            "JC",
            decision_type,
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )


def test_direct_dev_po_dev_and_stable_po_session(env):
    po = direct_handoff(env, "PO", "PRODUCT_CLARIFICATION")
    response = returned_response(env, po.request_dispatch_id, "Clarification PO")
    result = accept(
        env,
        po,
        response.response_id,
        decision_type="PRODUCT_CLARIFICATION",
    )
    assert result.status is HandoffStatus.RESUME_PREPARED
    with env["uow"]() as uow:
        request = uow.prompt_dispatches.get(po.request_dispatch_id)
        resume = uow.prompt_dispatches.get(result.resume_dispatch_id)
        decision = uow.decisions.for_handoff(po.handoff_id)
        assert request.agent_session == f"DevCockpit:PO:{KEY}"
        assert resume.agent_session == f"DevCockpit:DEV:{KEY}"
        assert decision.decision_type == "PRODUCT_CLARIFICATION"

    # A later direct PO consultation reuses the same logical AgentSession.
    from app.application.handoffs import CancelHandoff, cancel_handoff
    # RESUME_PREPARED is terminal; the active slot is already free.
    second = direct_handoff(env, "PO", "PRODUCT_CLARIFICATION")
    with env["uow"]() as uow:
        assert (
            uow.prompt_dispatches.get(second.request_dispatch_id).agent_session
            == f"DevCockpit:PO:{KEY}"
        )


@pytest.mark.parametrize(
    ("role", "purpose"),
    [
        ("ARCH", "PRODUCT_CLARIFICATION"),
        ("PO", "TECHNICAL_GUIDANCE"),
        ("DEV", "PRODUCT_CLARIFICATION"),
    ],
)
def test_invalid_role_purpose_pairs_are_rejected(env, role, purpose):
    with pytest.raises(ValueError):
        direct_handoff(env, role, purpose)


def test_arch_open_transfer_to_po_then_po_resumes_arch(env):
    arch = direct_handoff(env)
    arch_response = returned_response(env, arch.request_dispatch_id, "Consulter le PO")
    po = transfer_handoff_to_po(
        arch.handoff_id,
        TransferHandoffToPO(
            uuid4(),
            arch.version,
            "Clarifier la frontière produit",
            "Contexte ARCH",
            "JC",
            source_response_id=arch_response.response_id,
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    with env["uow"]() as uow:
        previous = uow.handoffs.get(arch.handoff_id)
        assert previous.status is HandoffStatus.TRANSFERRED
        assert po.status is HandoffStatus.OPEN
        assert po.predecessor_handoff_id == arch.handoff_id
        old_arch_session = uow.prompt_dispatches.get(arch.request_dispatch_id).agent_session

    po_response = returned_response(env, po.request_dispatch_id, "Clarification acceptée")
    resolved_po = accept(
        env,
        po,
        po_response.response_id,
        decision_type="PRODUCT_CLARIFICATION",
    )
    assert resolved_po.status is HandoffStatus.RESUME_PREPARED
    with env["uow"]() as uow:
        active = uow.handoffs.active(PROJECT.project_id, KEY)
        assert active is not None
        assert active.target_role == "ARCH"
        assert active.predecessor_handoff_id == po.handoff_id
        assert active.context_decision_id is not None
        new_arch_session = uow.prompt_dispatches.get(active.request_dispatch_id).agent_session
        assert new_arch_session == old_arch_session
        prepared_dev = [
            item for item in uow.prompt_dispatches.list_prepared()
            if item.project_id == PROJECT.project_id
            and item.work_item_id == KEY
            and item.role.value == "DEV"
        ]
        assert prepared_dev == []

    view = read_orchestration(
        PROJECT,
        KEY,
        roadmap_reader=env["roadmap"],
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )
    po_view = next(item for item in view["handoffs"] if item["handoff_id"] == po.handoff_id)
    assert po_view["resume_role"] == "ARCH"
    assert view["actions"]["automatic_dev_inhibited"]


def test_arch_hold_decision_can_be_explicit_po_context(env):
    arch = direct_handoff(env)
    response = returned_response(env, arch.request_dispatch_id)
    held = accept(
        env,
        arch,
        response.response_id,
        decision_type="ARCHITECTURE_GUIDANCE",
        effect=DecisionEffect.HOLD_FOR_AUTHORIZATION,
    )
    with env["uow"]() as uow:
        decision = uow.decisions.for_handoff(arch.handoff_id)

    po = transfer_handoff_to_po(
        arch.handoff_id,
        TransferHandoffToPO(
            uuid4(),
            held.version,
            "Décider le scope",
            "La Decision ARCH demande une autorisation",
            "JC",
            purpose="ROADMAP_REVIEW",
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    assert po.context_decision_id == decision.decision_id
    assert po.source_response_id == decision.source_response_id


def test_open_arch_transfer_requires_selected_response(env):
    arch = direct_handoff(env)
    with pytest.raises(OrchestrationConflict, match="selected response"):
        transfer_handoff_to_po(
            arch.handoff_id,
            TransferHandoffToPO(
                uuid4(),
                arch.version,
                "Question PO",
                "Context",
                "JC",
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )


def test_invalid_decision_type_for_po_is_rejected(env):
    po = direct_handoff(env, "PO", "PRODUCT_CLARIFICATION")
    response = returned_response(env, po.request_dispatch_id)
    with pytest.raises(ValueError, match="not allowed"):
        accept(
            env,
            po,
            response.response_id,
            decision_type="SCOPE_DECISION",
        )


def test_arch_to_po_transfer_is_atomic_on_failure(env, monkeypatch):
    from app.infrastructure.handoffs import SqlAlchemyHandoffRepository

    arch = direct_handoff(env)
    response = returned_response(env, arch.request_dispatch_id)
    original_add = SqlAlchemyHandoffRepository.add

    def fail_add(*args, **kwargs):
        raise RuntimeError("injected transfer failure")

    monkeypatch.setattr(SqlAlchemyHandoffRepository, "add", fail_add)
    with pytest.raises(RuntimeError, match="injected"):
        transfer_handoff_to_po(
            arch.handoff_id,
            TransferHandoffToPO(
                uuid4(),
                arch.version,
                "PO?",
                "Context",
                "JC",
                source_response_id=response.response_id,
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )
    monkeypatch.setattr(SqlAlchemyHandoffRepository, "add", original_add)

    with env["uow"]() as uow:
        current = uow.handoffs.get(arch.handoff_id)
        assert current.status is HandoffStatus.OPEN
        handoffs = uow.handoffs.list_for_work_item(PROJECT.project_id, KEY)
        assert len(handoffs) == 1
        po_dispatches = [
            item for item in uow.prompt_dispatches.list_for_work_item(PROJECT.project_id, KEY)
            if item.role.value == "PO"
        ]
        assert po_dispatches == []


def test_po_to_arch_resolution_is_atomic_on_failure(env, monkeypatch):
    from app.infrastructure.handoffs import SqlAlchemyHandoffRepository

    arch = direct_handoff(env)
    arch_response = returned_response(env, arch.request_dispatch_id)
    po = transfer_handoff_to_po(
        arch.handoff_id,
        TransferHandoffToPO(
            uuid4(),
            arch.version,
            "PO?",
            "Context",
            "JC",
            source_response_id=arch_response.response_id,
        ),
        project_catalog=env["catalog"],
        roadmap_reader=env["roadmap"],
        uow_factory=env["uow"],
    )
    po_response = returned_response(env, po.request_dispatch_id)
    original_add = SqlAlchemyHandoffRepository.add

    def fail_add(*args, **kwargs):
        raise RuntimeError("injected resume failure")

    monkeypatch.setattr(SqlAlchemyHandoffRepository, "add", fail_add)
    with pytest.raises(RuntimeError, match="injected"):
        accept(
            env,
            po,
            po_response.response_id,
            decision_type="PRODUCT_CLARIFICATION",
        )
    monkeypatch.setattr(SqlAlchemyHandoffRepository, "add", original_add)

    with env["uow"]() as uow:
        current = uow.handoffs.get(po.handoff_id)
        assert current.status is HandoffStatus.OPEN
        assert uow.decisions.for_handoff(po.handoff_id) is None
        linked_arch = [
            item for item in uow.handoffs.list_for_work_item(PROJECT.project_id, KEY)
            if item.predecessor_handoff_id == po.handoff_id
        ]
        assert linked_arch == []


def test_arch_to_po_transfer_and_execution_poller_never_expose_dev_gap(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.application.executions import evaluate_project_execution

    arch = direct_handoff(env)
    arch_response = returned_response(env, arch.request_dispatch_id)
    barrier = Barrier(2)
    command = TransferHandoffToPO(
        uuid4(),
        arch.version,
        "PO?",
        "Context",
        "JC",
        source_response_id=arch_response.response_id,
    )

    def do_transfer():
        barrier.wait()
        return transfer_handoff_to_po(
            arch.handoff_id,
            command,
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )

    def do_poll():
        barrier.wait()
        return evaluate_project_execution(
            PROJECT,
            roadmap_reader=env["roadmap"],
            evidence_reader=env["evidence"],
            uow_factory=env["uow"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        transfer_result, _ = [
            future.result()
            for future in (
                pool.submit(do_transfer),
                pool.submit(do_poll),
            )
        ]

    assert transfer_result.target_role == "PO"
    with env["uow"]() as uow:
        active = uow.handoffs.active(PROJECT.project_id, KEY)
        assert active is not None and active.target_role == "PO"
        assert [
            item for item in uow.prompt_dispatches.list_prepared()
            if item.project_id == PROJECT.project_id
            and item.work_item_id == KEY
            and item.role.value == "DEV"
        ] == []


def test_arch_open_transfer_rejects_response_from_wrong_dispatch(env):
    arch = direct_handoff(env)
    unrelated = create_prompt_dispatch(
        CreatePromptDispatchCommand(
            PROJECT.project_id,
            KEY,
            "ARCH",
            "unrelated ARCH prompt",
            str(uuid4()),
        ),
        uow_factory=env["uow"],
    )
    wrong_response = returned_response(env, unrelated.dispatch_id, "wrong source")
    with pytest.raises(OrchestrationConflict, match="does not originate"):
        transfer_handoff_to_po(
            arch.handoff_id,
            TransferHandoffToPO(
                uuid4(),
                arch.version,
                "PO?",
                "Context",
                "JC",
                source_response_id=wrong_response.response_id,
            ),
            project_catalog=env["catalog"],
            roadmap_reader=env["roadmap"],
            uow_factory=env["uow"],
        )
