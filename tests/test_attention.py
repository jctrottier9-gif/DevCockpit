from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.application.attention import (
    AttentionAction,
    AttentionItem,
    AttentionKind,
    AttentionLevel,
    AttentionProjection,
    AttentionState,
    read_project_attention,
)
from app.application.chatgpt_responses import (
    ImportChatGptResponseCommand,
    import_chatgpt_response,
)
from app.application.handoffs import CreateHandoff, create_handoff
from app.application.parallel_executions import evaluate_project_parallel_dev_executions
from app.application.projects import ProjectCatalog
from app.application.prompt_deliveries import acknowledge_prompt_delivery, prepare_prompt_deliveries_for_send
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.chatgpt_prompt_send import ChatGptPromptSend, ChatGptPromptSendState
from app.domain.execution import (
    ExecutionEvidence,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.project import Project
from app.domain.resource_lock import (
    ResourceLockMode,
    ResourceLockRequirement,
    WorkItemResourceLockDeclaration,
)
from app.infrastructure.database import upgrade_database
from app.main import create_app


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
DC-060 | WORK | READY | #1 | MAIN | Attention Center | - | -
DC-061 | WORK | BLOCKED | #1 | MAIN | Flow Analytics | - | DC-060
<!-- /COCKPIT_PIPELINE_V3 -->"""


class RoadmapReader:
    def __init__(self, body: str = ROADMAP):
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            "2026-10-02T16:00:00Z",
        )


class CleanEvidence:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(default_branch="main")


class RedEvidence:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(
            default_branch="main",
            pull_requests=(
                PullRequestEvidence(
                    number=40,
                    title=f"{work_item.key} — implementation",
                    body="",
                    branch=f"{work_item.key.lower()}-implementation",
                    head_sha="abc123",
                    state="open",
                    merged=False,
                    mergeable=False,
                    url="https://github.example/pr/40",
                ),
            ),
            workflow_runs=(
                WorkflowRunEvidence(
                    run_id=400,
                    name="CI",
                    status="completed",
                    conclusion="failure",
                    attempt=1,
                    head_sha="abc123",
                    url="https://github.example/actions/400",
                    failed_jobs=("backend",),
                ),
            ),
        )


class MergedGreenEvidence:
    def read(self, project, work_item) -> ExecutionEvidence:
        return ExecutionEvidence(
            default_branch="main",
            pull_requests=(
                PullRequestEvidence(
                    number=41,
                    title=f"{work_item.key} — implementation",
                    body="",
                    branch=f"{work_item.key.lower()}-implementation",
                    head_sha="def456",
                    state="closed",
                    merged=True,
                    mergeable=True,
                    url="https://github.example/pr/41",
                    merged_at="2026-10-02T16:05:00Z",
                ),
            ),
            workflow_runs=(
                WorkflowRunEvidence(
                    run_id=410,
                    name="CI",
                    status="completed",
                    conclusion="success",
                    attempt=1,
                    head_sha="def456",
                    url="https://github.example/actions/410",
                ),
            ),
        )


def application(tmp_path, *, project=PROJECT, roadmap=None, evidence=None, capacity=2):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / (project.project_id + '.db')}",
        execution_poll_seconds=0,
        max_parallel_dev_executions=capacity,
    )
    upgrade_database(settings)
    app = create_app(
        settings,
        project_catalog=ProjectCatalog([project]),
        roadmap_reader=roadmap or RoadmapReader(),
        execution_reader=evidence or CleanEvidence(),
    )
    return app


def projection(app, project=PROJECT, *, companion_connected=False):
    return read_project_attention(
        project,
        roadmap_reader=app.state.roadmap_reader,
        evidence_reader=app.state.execution_reader,
        uow_factory=app.state.uow_factory,
        max_parallel_dev_executions=app.state.settings.max_parallel_dev_executions,
        companion_connected=companion_connected,
    )


def prepared_prompt(app, *, project_id="DevCockpit", work_item="DC-060", role="DEV", key=None):
    return create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id=project_id,
            work_item_id=work_item,
            role=role,
            prompt_text=f"{role} prompt for {work_item}",
            idempotency_key=key or f"test:{project_id}:{role}:{work_item}:{uuid4()}",
        ),
        uow_factory=app.state.uow_factory,
    )


def import_response_for(app, dispatch, *, text="Returned response"):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=app.state.uow_factory)
    delivery = next(item for item in outbound if item.dispatch_id == dispatch.dispatch_id)
    command = ImportChatGptResponseCommand(
        response_id=uuid4(),
        delivery_id=delivery.delivery_id,
        session=delivery.session,
        text=text,
    )
    return import_chatgpt_response(command, uow_factory=app.state.uow_factory)


def set_send_state(app, dispatch, state: ChatGptPromptSendState):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=app.state.uow_factory)
    delivery = next(item for item in outbound if item.dispatch_id == dispatch.dispatch_id)
    acknowledge_prompt_delivery(delivery.delivery_id, uow_factory=app.state.uow_factory)
    now = datetime.now(timezone.utc)
    with app.state.uow_factory() as uow:
        uow.chatgpt_prompt_sends.save(
            ChatGptPromptSend.rehydrate(
                delivery_id=delivery.delivery_id,
                session=delivery.session,
                state=state,
                attempt_count=1,
                last_error_code=None,
                next_retry_at=None,
                confirmed_at=now if state is ChatGptPromptSendState.SENT_CONFIRMED else None,
                updated_at=now,
                last_event_id=None,
            )
        )
        uow.commit()
    return delivery


def test_clear_is_explicit_when_no_signal_exists(tmp_path):
    app = application(tmp_path)

    result = projection(app)

    assert result.state is AttentionState.CLEAR
    assert result.action_count == 0
    assert result.watch_count == 0
    assert result.items == ()

    response = TestClient(app).get("/api/projects/DevCockpit/attention")
    assert response.status_code == 200
    assert response.json() == {
        "state": "CLEAR",
        "counts": {"action": 0, "watch": 0},
        "items": [],
    }


def test_prepared_prompt_is_actionable_and_transport_only_alerts_when_it_blocks(tmp_path):
    app = application(tmp_path)
    dispatch = prepared_prompt(app)

    connected = projection(app, companion_connected=True)
    assert connected.state is AttentionState.ACTION
    assert len(connected.items) == 1
    item = connected.items[0]
    assert item.kind is AttentionKind.PROMPT_READY
    assert item.role == "DEV"
    assert item.work_item_id == "DC-060"
    assert item.agent_session == "DevCockpit:DEV:DC-060"
    assert item.primary_action.kind == "SEND_PROMPT"
    assert item.context["dispatch_id"] == str(dispatch.dispatch_id)

    disconnected = projection(app, companion_connected=False)
    assert len(disconnected.items) == 1
    blocked = disconnected.items[0]
    assert blocked.stable_key == item.stable_key
    assert blocked.kind is AttentionKind.TRANSPORT_BLOCKED
    assert blocked.primary_action.kind == "CONNECT_COMPANION"
    assert blocked.context["transport_connected"] is False


def test_sent_confirmed_is_watch_with_no_imported_response_claim(tmp_path):
    app = application(tmp_path)
    dispatch = prepared_prompt(app)
    delivery = set_send_state(app, dispatch, ChatGptPromptSendState.SENT_CONFIRMED)

    result = projection(app, companion_connected=True)

    assert result.state is AttentionState.WATCH
    assert result.action_count == 0
    assert result.watch_count == 1
    item = result.items[0]
    assert item.level is AttentionLevel.WATCH
    assert item.kind is AttentionKind.CHATGPT_SEND
    assert item.primary_action.kind == "WAIT_IMPORTED_RESPONSE"
    assert item.context["delivery_id"] == str(delivery.delivery_id)
    assert item.context["interaction_state"] == "SENT_CONFIRMED"
    assert item.context["imported_response_available"] is False
    assert "Aucune réponse" in item.primary_action.label


def test_ambiguous_send_is_action_and_never_allows_resend(tmp_path):
    app = application(tmp_path)
    dispatch = prepared_prompt(app)
    set_send_state(app, dispatch, ChatGptPromptSendState.AMBIGUOUS)

    result = projection(app, companion_connected=True)

    assert result.state is AttentionState.ACTION
    item = result.items[0]
    assert item.level is AttentionLevel.ACTION
    assert item.kind is AttentionKind.CHATGPT_SEND
    assert item.primary_action.kind == "RECONCILE_CHATGPT_SEND"
    assert item.context["interaction_state"] == "AMBIGUOUS"
    assert item.context["automatic_resend_allowed"] is False
    assert "aucun renvoi automatique" in item.reason


def test_disconnected_companion_without_ready_prompt_does_not_create_noise(tmp_path):
    app = application(tmp_path)

    result = projection(app, companion_connected=False)

    assert result.state is AttentionState.CLEAR
    assert result.items == ()


def test_ci_red_and_ready_corrective_prompt_are_one_logical_action(tmp_path):
    app = application(tmp_path, evidence=RedEvidence())
    dispatch = prepared_prompt(app, role="DEV")

    result = projection(app, companion_connected=True)

    assert result.state is AttentionState.ACTION
    assert len(result.items) == 1
    item = result.items[0]
    assert item.stable_key == "DevCockpit:DEV:DC-060:FIX_CI"
    assert item.kind is AttentionKind.CI_RED
    assert item.primary_action.kind == "FIX_CI"
    assert item.pr_number == 40
    assert {e.source for e in item.evidence} == {
        "ExecutionProjection",
        "PromptDispatch",
    }
    assert str(dispatch.dispatch_id) in {e.identity for e in item.evidence}


def test_ci_red_transport_block_keeps_one_card_and_makes_reconnect_primary(tmp_path):
    app = application(tmp_path, evidence=RedEvidence())
    prepared_prompt(app, role="DEV")

    result = projection(app, companion_connected=False)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.stable_key == "DevCockpit:DEV:DC-060:FIX_CI"
    assert item.kind is AttentionKind.TRANSPORT_BLOCKED
    assert item.primary_action.kind == "CONNECT_COMPANION"
    assert item.pr_number == 40
    assert {e.source for e in item.evidence} == {
        "ExecutionProjection",
        "PromptDispatch",
    }


def test_arch_response_becomes_arch_review_action_not_duplicate_prompt(tmp_path):
    app = application(tmp_path)
    source = prepared_prompt(app)
    handoff = create_handoff(
        PROJECT,
        "DC-060",
        CreateHandoff(
            creation_command_id=uuid4(),
            source_dispatch_id=source.dispatch_id,
            question="Quelle architecture retenir?",
            context="Contexte confirmé",
            created_by="JC",
        ),
        roadmap_reader=app.state.roadmap_reader,
        uow_factory=app.state.uow_factory,
    )
    with app.state.uow_factory() as uow:
        request = uow.prompt_dispatches.get(handoff.request_dispatch_id)
    import_response_for(app, request, text="Réponse architecte")

    result = projection(app, companion_connected=True)

    arch = [item for item in result.items if item.role == "ARCH"]
    assert len(arch) == 1
    assert arch[0].kind is AttentionKind.HANDOFF
    assert arch[0].primary_action.kind == "REVIEW_DECISION"
    assert arch[0].work_item_id == "DC-060"


def test_po_response_becomes_po_review_action(tmp_path):
    app = application(tmp_path)
    source = prepared_prompt(app)
    handoff = create_handoff(
        PROJECT,
        "DC-060",
        CreateHandoff(
            creation_command_id=uuid4(),
            source_dispatch_id=source.dispatch_id,
            question="Quelle clarification produit retenir?",
            context="Contexte confirmé",
            created_by="JC",
            target_role="PO",
            purpose="PRODUCT_CLARIFICATION",
        ),
        roadmap_reader=app.state.roadmap_reader,
        uow_factory=app.state.uow_factory,
    )
    with app.state.uow_factory() as uow:
        request = uow.prompt_dispatches.get(handoff.request_dispatch_id)
    import_response_for(app, request, text="Réponse PO")

    result = projection(app, companion_connected=True)

    po = [item for item in result.items if item.role == "PO"]
    assert len(po) == 1
    assert po[0].primary_action.kind == "REVIEW_DECISION"
    assert po[0].work_item_id == "DC-060"


def test_distinct_actions_on_same_work_item_are_not_merged(tmp_path):
    app = application(tmp_path, evidence=RedEvidence())
    source = prepared_prompt(app)
    handoff = create_handoff(
        PROJECT,
        "DC-060",
        CreateHandoff(
            creation_command_id=uuid4(),
            source_dispatch_id=source.dispatch_id,
            question="Analyser le blocage",
            context="CI rouge et choix architecture",
            created_by="JC",
        ),
        roadmap_reader=app.state.roadmap_reader,
        uow_factory=app.state.uow_factory,
    )
    with app.state.uow_factory() as uow:
        request = uow.prompt_dispatches.get(handoff.request_dispatch_id)
    import_response_for(app, request)

    result = projection(app, companion_connected=True)
    actions = {(item.role, item.primary_action.kind) for item in result.items}

    assert ("DEV", "FIX_CI") in actions
    assert ("ARCH", "REVIEW_DECISION") in actions
    assert len(result.items) == 2


def test_roadmap_update_required_is_explicit_and_links_pr_evidence(tmp_path):
    app = application(tmp_path, evidence=MergedGreenEvidence())

    result = projection(app, companion_connected=True)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.kind is AttentionKind.ROADMAP_UPDATE_REQUIRED
    assert item.primary_action.kind == "RECONCILE_ROADMAP"
    assert item.pr_number == 41
    assert item.pr_url == "https://github.example/pr/41"


def test_automatic_roadmap_reconciliation_prompt_replaces_plain_attention_action(tmp_path):
    app = application(tmp_path, evidence=MergedGreenEvidence())

    evaluation = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=app.state.roadmap_reader,
        evidence_reader=app.state.execution_reader,
        uow_factory=app.state.uow_factory,
        max_parallel_dev_executions=2,
    )
    assert len(evaluation.dispatches) == 1

    result = projection(app, companion_connected=True)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.stable_key == "DevCockpit:DEV:DC-060:RECONCILE_ROADMAP"
    assert item.kind is AttentionKind.ROADMAP_UPDATE_REQUIRED
    assert item.title == "DEV · DC-060 · réconciliation roadmap prête"
    assert item.primary_action.kind == "RECONCILE_ROADMAP"
    assert item.primary_action.dispatch_id == str(
        evaluation.dispatches[0].dispatch_id
    )
    assert item.pr_number == 41
    assert item.context["dispatch_id"] == str(
        evaluation.dispatches[0].dispatch_id
    )


def test_imported_response_naturally_removes_prompt_attention_without_dismiss(tmp_path):
    app = application(tmp_path)
    dispatch = prepared_prompt(app)

    before = projection(app, companion_connected=True)
    assert len(before.items) == 1

    import_response_for(app, dispatch)
    after = projection(app, companion_connected=True)

    assert after.state is AttentionState.CLEAR
    assert after.items == ()


LOCKED_PROJECT = Project(
    "Locked",
    "jctrottier9-gif/DevCockpit",
    1,
    resource_locks=(
        WorkItemResourceLockDeclaration(
            "A",
            (ResourceLockRequirement.build("migration:alembic", ResourceLockMode.EXCLUSIVE),),
        ),
        WorkItemResourceLockDeclaration(
            "B",
            (ResourceLockRequirement.build("migration:alembic", ResourceLockMode.EXCLUSIVE),),
        ),
    ),
)
LOCKED_ROADMAP = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | READY | #1 | MAIN | A | - | -
B | WORK | READY | #1 | AUX | B | - | -
<!-- /COCKPIT_PIPELINE_V3 -->"""


def test_resource_lock_conflict_is_explainable_from_existing_projection(tmp_path):
    app = application(
        tmp_path,
        project=LOCKED_PROJECT,
        roadmap=RoadmapReader(LOCKED_ROADMAP),
        evidence=CleanEvidence(),
        capacity=2,
    )
    evaluation = evaluate_project_parallel_dev_executions(
        LOCKED_PROJECT,
        roadmap_reader=app.state.roadmap_reader,
        evidence_reader=app.state.execution_reader,
        uow_factory=app.state.uow_factory,
        max_parallel_dev_executions=2,
        resource_lock_lease_seconds=900,
        lease_owner_id="attention-test",
    )
    assert [dispatch.work_item_id for dispatch in evaluation.dispatches] == ["A"]

    result = projection(app, LOCKED_PROJECT, companion_connected=True)
    lock_item = next(
        item for item in result.items
        if item.kind is AttentionKind.RESOURCE_LOCK_CONFLICT
    )

    assert lock_item.work_item_id == "B"
    assert lock_item.context == {
        "surface": "migration:alembic",
        "requested_mode": "EXCLUSIVE",
        "holder_work_item_id": "A",
        "holder_agent_session": "Locked:DEV:A",
        "holder_mode": "EXCLUSIVE",
        "holder_state": "ACTIVE",
        "reason": "INCOMPATIBLE_RESOURCE_LOCK",
    }
    assert lock_item.primary_action.kind == "RESOLVE_RESOURCE_LOCK"


def test_attention_order_is_deterministic(tmp_path):
    body = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
B | WORK | READY | #1 | MAIN | B | - | -
A | WORK | READY | #1 | AUX | A | - | -
<!-- /COCKPIT_PIPELINE_V3 -->"""
    project = Project("Ordering", "jctrottier9-gif/DevCockpit", 1)
    app = application(
        tmp_path,
        project=project,
        roadmap=RoadmapReader(body),
        capacity=2,
    )
    prepared_prompt(app, project_id="Ordering", work_item="B", role="DEV")
    prepared_prompt(app, project_id="Ordering", work_item="A", role="ARCH")

    first = projection(app, project, companion_connected=True)
    second = projection(app, project, companion_connected=True)

    assert [item.stable_key for item in first.items] == [
        "Ordering:ARCH:A:SEND_PROMPT",
        "Ordering:DEV:B:SEND_PROMPT",
    ]
    assert first.items == second.items


def test_attention_is_isolated_by_project(tmp_path):
    p1 = Project("P1", "jctrottier9-gif/DevCockpit", 1)
    p2 = Project("P2", "jctrottier9-gif/DevCockpit", 1)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'multi.db'}",
        execution_poll_seconds=0,
        max_parallel_dev_executions=2,
    )
    upgrade_database(settings)
    app = create_app(
        settings,
        project_catalog=ProjectCatalog([p1, p2]),
        roadmap_reader=RoadmapReader(),
        execution_reader=CleanEvidence(),
    )
    prepared_prompt(app, project_id="P1")
    prepared_prompt(app, project_id="P2")

    response = TestClient(app).get("/api/projects/P1/attention")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "ACTION"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["project_id"] == "P1"
    assert payload["items"][0]["context"]["transport_connected"] is False


def test_api_serializes_watch_state_when_projection_contains_watch(tmp_path, monkeypatch):
    app = application(tmp_path)
    watch = AttentionItem(
        stable_key="DevCockpit:PO:DC-060:WAIT_ROADMAP_APPLICATION",
        level=AttentionLevel.WATCH,
        kind=AttentionKind.ROADMAP_APPLICATION,
        title="PO · DC-060 · application roadmap en cours",
        reason="Application en cours.",
        project_id="DevCockpit",
        work_item_id="DC-060",
        role="PO",
        agent_session="DevCockpit:PO:DC-060",
        primary_action=AttentionAction(
            kind="WAIT_ROADMAP_APPLICATION",
            label="Ouvrir l'application roadmap",
            target="orchestration",
            work_item_id="DC-060",
        ),
    )

    monkeypatch.setattr(
        "app.api.attention.read_project_attention",
        lambda *args, **kwargs: AttentionProjection(
            state=AttentionState.WATCH,
            action_count=0,
            watch_count=1,
            items=(watch,),
        ),
    )

    response = TestClient(app).get("/api/projects/DevCockpit/attention")

    assert response.status_code == 200
    assert response.json()["state"] == "WATCH"
    assert response.json()["counts"] == {"action": 0, "watch": 1}
    assert response.json()["items"][0]["level"] == "WATCH"
