from app.application.parallel_executions import (
    DevExecutionSlotState,
    evaluate_project_parallel_dev_executions,
    read_project_parallel_dev_executions,
)
from app.application.roadmaps import RoadmapIssue
from app.domain.execution import (
    ExecutionEvidence,
    ExecutionState,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.project import Project


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


def v3(*rows: str) -> str:
    return "\n".join(
        [
            "<!-- COCKPIT_PIPELINE_V3 -->",
            "KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON",
            *rows,
            "<!-- /COCKPIT_PIPELINE_V3 -->",
        ]
    )


class RoadmapReader:
    def __init__(self, body: str) -> None:
        self.body = body

    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            self.body,
            "2026-10-02T12:00:00Z",
        )


class EvidenceReader:
    def __init__(self, by_work_item=None) -> None:
        self.by_work_item = by_work_item or {}
        self.calls: list[str] = []

    def read(self, project, work_item) -> ExecutionEvidence:
        self.calls.append(work_item.key)
        return self.by_work_item.get(
            work_item.key,
            ExecutionEvidence(default_branch="main"),
        )


class DispatchRepository:
    def __init__(self) -> None:
        self.by_key = {}
        self.by_id = {}

    def add(self, dispatch) -> None:
        self.by_key[dispatch.idempotency_key] = dispatch
        self.by_id[dispatch.dispatch_id] = dispatch

    def get(self, dispatch_id):
        return self.by_id.get(dispatch_id)

    def get_by_idempotency_key(self, idempotency_key):
        return self.by_key.get(idempotency_key)


class Handoffs:
    def __init__(self, active_keys=()) -> None:
        self.active_keys = set(active_keys)

    def active(self, project_id, work_item_id):
        return object() if work_item_id in self.active_keys else None

    def covers(self, project_id, work_item_id, evidence_key):
        return False


class Fences:
    class Snapshot:
        def __init__(self, generation=0, active_application_id=None):
            self.generation = generation
            self.active_application_id = active_application_id

    def __init__(self) -> None:
        self.generation = 0
        self.active_application_id = None

    def snapshot(self, repository_full_name, roadmap_issue_number):
        return self.Snapshot(self.generation, self.active_application_id)


class Uow:
    def __init__(self, dispatches, handoffs, fences) -> None:
        self.prompt_dispatches = dispatches
        self.handoffs = handoffs
        self.roadmap_target_fences = fences

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


def factory(dispatches=None, *, handoffs=None, fences=None):
    shared_dispatches = dispatches or DispatchRepository()
    shared_handoffs = handoffs or Handoffs()
    shared_fences = fences or Fences()
    return (
        shared_dispatches,
        lambda: Uow(shared_dispatches, shared_handoffs, shared_fences),
    )


def pr(work_item: str, *, head_sha: str) -> PullRequestEvidence:
    return PullRequestEvidence(
        number=1 if work_item == "A" else 2,
        title=f"{work_item} — implementation",
        body="",
        branch=f"{work_item.lower()}-implementation",
        head_sha=head_sha,
        state="open",
        merged=False,
        mergeable=False,
        url=f"https://github.example/{work_item}",
    )


def test_two_ready_work_items_start_in_parallel_with_distinct_sessions():
    dispatches, uow_factory = factory()
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
            )
        ),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    assert [item.execution.work_item.key for item in result.projection.items] == ["A", "B"]
    assert result.projection.active_count == 2
    assert [item.slot_state for item in result.projection.items] == [
        DevExecutionSlotState.ACTIVE,
        DevExecutionSlotState.ACTIVE,
    ]
    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A", "B"]
    assert result.dispatches[0].agent_session == "DevCockpit:DEV:A"
    assert result.dispatches[1].agent_session == "DevCockpit:DEV:B"
    assert result.dispatches[0].agent_session != result.dispatches[1].agent_session
    assert len(dispatches.by_key) == 2


def test_capacity_selects_canonical_order_and_leaves_third_waiting():
    dispatches, uow_factory = factory()
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
                "C | WORK | READY | #1 | AUX2 | C | - | -",
            )
        ),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    by_key = {item.execution.work_item.key: item for item in result.projection.items}
    assert result.projection.active_count == 2
    assert result.projection.available_capacity == 0
    assert by_key["A"].active
    assert by_key["B"].active
    assert by_key["C"].slot_state is DevExecutionSlotState.WAITING_FOR_CAPACITY
    assert by_key["C"].waiting_for_capacity
    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A", "B"]


def test_ci_red_follow_up_is_isolated_from_running_peer():
    evidence = EvidenceReader(
        {
            "A": ExecutionEvidence(
                default_branch="main",
                pull_requests=(pr("A", head_sha="aaa"),),
                workflow_runs=(
                    WorkflowRunEvidence(
                        run_id=101,
                        name="CI",
                        status="completed",
                        conclusion="failure",
                        attempt=1,
                        head_sha="aaa",
                        failed_jobs=("backend",),
                    ),
                ),
            ),
            "B": ExecutionEvidence(
                default_branch="main",
                pull_requests=(pr("B", head_sha="bbb"),),
                workflow_runs=(
                    WorkflowRunEvidence(
                        run_id=202,
                        name="CI",
                        status="in_progress",
                        conclusion=None,
                        attempt=1,
                        head_sha="bbb",
                    ),
                ),
            ),
        }
    )
    dispatches, uow_factory = factory()
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
            )
        ),
        evidence_reader=evidence,
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    by_key = {item.execution.work_item.key: item for item in result.projection.items}
    assert by_key["A"].execution.state is ExecutionState.CI_RED
    assert by_key["B"].execution.state is ExecutionState.CI_RUNNING
    assert len(result.dispatches) == 1
    assert result.dispatches[0].work_item_id == "A"
    assert result.dispatches[0].agent_session == "DevCockpit:DEV:A"
    assert all(dispatch.work_item_id != "B" for dispatch in dispatches.by_key.values())


def test_repoll_and_restart_reconstruct_active_slots_without_duplicate_initial_prompts():
    dispatches, first_factory = factory()
    roadmap = RoadmapReader(
        v3(
            "A | WORK | READY | #1 | MAIN | A | - | -",
            "B | WORK | READY | #1 | AUX | B | - | -",
        )
    )
    first = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=first_factory,
        max_parallel_dev_executions=2,
    )

    _, restarted_factory = factory(dispatches)
    second = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=restarted_factory,
        max_parallel_dev_executions=2,
    )

    assert len(first.dispatches) == 2
    assert second.dispatches == ()
    assert second.projection.active_count == 2
    assert all(item.active for item in second.projection.items)
    assert len(dispatches.by_key) == 2


def test_blocked_and_unsatisfied_work_items_never_consume_capacity_or_dispatch():
    dispatches, uow_factory = factory()
    evidence = EvidenceReader()
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | BLOCKED | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | A",
            )
        ),
        evidence_reader=evidence,
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    assert result.projection.items == ()
    assert result.projection.active_count == 0
    assert result.dispatches == ()
    assert dispatches.by_key == {}
    assert evidence.calls == []


def test_handoff_inhibits_only_its_work_item_and_does_not_block_independent_peer():
    dispatches, uow_factory = factory(handoffs=Handoffs({"A"}))
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
            )
        ),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
    )

    by_key = {item.execution.work_item.key: item for item in result.projection.items}
    assert by_key["A"].slot_state is DevExecutionSlotState.INHIBITED
    assert by_key["A"].inhibition_reason == "HANDOFF_ACTIVE"
    assert by_key["B"].active
    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["B"]


def test_read_projection_reports_existing_capacity_without_mutating_dispatches():
    dispatches, uow_factory = factory()
    first = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(v3("A | WORK | READY | #1 | MAIN | A | - | -")),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )
    assert len(first.dispatches) == 1

    read_back = read_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
            )
        ),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    by_key = {item.execution.work_item.key: item for item in read_back.items}
    assert read_back.active_count == 1
    assert by_key["A"].active
    assert by_key["B"].slot_state is DevExecutionSlotState.SELECTED
    assert len(dispatches.by_key) == 1



def test_active_roadmap_application_fence_inhibits_all_new_dispatches():
    fences = Fences()
    fences.generation = 3
    fences.active_application_id = "application-1"
    dispatches, uow_factory = factory(fences=fences)

    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3(
                "A | WORK | READY | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | AUX | B | - | -",
            )
        ),
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    assert result.dispatches == ()
    assert dispatches.by_key == {}
    assert [item.slot_state for item in result.projection.items] == [
        DevExecutionSlotState.INHIBITED,
        DevExecutionSlotState.INHIBITED,
    ]
    assert {
        item.inhibition_reason for item in result.projection.items
    } == {"ROADMAP_APPLICATION_FENCE"}
