from app.application.parallel_executions import (
    DevExecutionSlotState,
    evaluate_project_parallel_dev_executions,
    read_project_parallel_dev_executions,
)
from app.application.roadmaps import RoadmapIssue
from app.domain.execution import (
    BranchEvidence,
    ExecutionEvidence,
    ExecutionState,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.project import Project
from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole
from app.domain.resource_lock import (
    ResourceLock,
    ResourceLockConflict,
    ResourceLockMode,
    ResourceLockRequirement,
    ResourceLockState,
    WorkItemResourceLockDeclaration,
    lock_modes_compatible,
)
from datetime import datetime, timedelta, timezone


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


class ResourceLocks:
    def __init__(self) -> None:
        self.by_owner_surface = {}
        self.acquire_calls = 0

    def list_for_project(self, project_id):
        return tuple(
            lock for lock in self.by_owner_surface.values()
            if lock.project_id == project_id
        )

    def list_active_for_project(self, project_id):
        return tuple(
            lock for lock in self.list_for_project(project_id)
            if lock.state is ResourceLockState.ACTIVE
        )

    def list_for_owner(self, project_id, work_item_id):
        return tuple(
            lock for lock in self.list_for_project(project_id)
            if lock.work_item_id == work_item_id
        )

    def save(self, lock):
        self.by_owner_surface[
            (lock.project_id, lock.work_item_id, lock.surface.key)
        ] = lock

    def acquire_many(
        self,
        *,
        project_id,
        work_item_id,
        agent_session,
        lease_owner_id,
        requirements,
        now,
        lease_seconds,
    ):
        self.acquire_calls += 1
        requirements = tuple(requirements)
        for requirement in requirements:
            for holder in self.list_active_for_project(project_id):
                if holder.work_item_id == work_item_id:
                    continue
                if holder.surface != requirement.surface:
                    continue
                if not lock_modes_compatible(requirement.mode, holder.mode):
                    return (), ResourceLockConflict(
                        surface=requirement.surface,
                        requested_mode=requirement.mode,
                        holder_work_item_id=holder.work_item_id,
                        holder_agent_session=holder.agent_session,
                        holder_mode=holder.mode,
                        holder_state=holder.state,
                    )

        acquired = []
        for requirement in requirements:
            key = (project_id, work_item_id, requirement.surface.key)
            existing = self.by_owner_surface.get(key)
            if existing is None:
                lock = ResourceLock.acquire(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    agent_session=agent_session,
                    lease_owner_id=lease_owner_id,
                    requirement=requirement,
                    now=now,
                    lease_seconds=lease_seconds,
                )
            else:
                lock = existing.reactivate(
                    agent_session=agent_session,
                    lease_owner_id=lease_owner_id,
                    requirement=requirement,
                    now=now,
                    lease_seconds=lease_seconds,
                )
            self.save(lock)
            acquired.append(lock)
        return tuple(acquired), None


class Deliveries:
    def __init__(self) -> None:
        self.by_dispatch = {}

    def get_by_dispatch_id(self, dispatch_id):
        return self.by_dispatch.get(dispatch_id)

    def save(self, delivery):
        self.by_dispatch[delivery.dispatch_id] = delivery


class Uow:
    def __init__(self, dispatches, handoffs, fences, resource_locks, deliveries) -> None:
        self.prompt_dispatches = dispatches
        self.prompt_deliveries = deliveries
        self.handoffs = handoffs
        self.roadmap_target_fences = fences
        self.resource_locks = resource_locks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def commit(self):
        return None

    def flush(self):
        return None

    def rollback(self):
        return None


def factory(
    dispatches=None,
    *,
    handoffs=None,
    fences=None,
    resource_locks=None,
    deliveries=None,
):
    shared_dispatches = dispatches or DispatchRepository()
    shared_handoffs = handoffs or Handoffs()
    shared_fences = fences or Fences()
    shared_resource_locks = resource_locks or ResourceLocks()
    shared_deliveries = deliveries or Deliveries()
    return (
        shared_dispatches,
        lambda: Uow(
            shared_dispatches,
            shared_handoffs,
            shared_fences,
            shared_resource_locks,
            shared_deliveries,
        ),
    )


def project_with_locks(*declarations):
    return Project(
        "DevCockpit",
        "jctrottier9-gif/DevCockpit",
        1,
        resource_locks=tuple(
            WorkItemResourceLockDeclaration(
                work_item_id=work_item,
                requirements=tuple(
                    ResourceLockRequirement.build(surface, mode)
                    for surface, mode in requirements
                ),
            )
            for work_item, requirements in declarations
        ),
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


def test_merged_green_prepares_automatic_roadmap_reconciliation_in_same_dev_session():
    evidence = EvidenceReader(
        {
            "A": ExecutionEvidence(
                default_branch="main",
                pull_requests=(
                    PullRequestEvidence(
                        number=589,
                        title="A — implementation",
                        body="",
                        branch="a-implementation",
                        head_sha="delivered-sha",
                        state="closed",
                        merged=True,
                        mergeable=True,
                        url="https://github.example/pr/589",
                        merged_at="2026-10-02T22:00:00Z",
                    ),
                ),
                workflow_runs=(
                    WorkflowRunEvidence(
                        run_id=1314,
                        name="CI",
                        status="completed",
                        conclusion="success",
                        attempt=1,
                        head_sha="delivered-sha",
                    ),
                ),
            ),
        }
    )
    dispatches, uow_factory = factory()

    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3("A | WORK | READY | #1 | MAIN | A | - | -")
        ),
        evidence_reader=evidence,
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
    )

    assert result.projection.items[0].execution.state is ExecutionState.ROADMAP_UPDATE_REQUIRED
    assert len(result.dispatches) == 1
    dispatch = result.dispatches[0]
    assert dispatch.work_item_id == "A"
    assert dispatch.agent_session == "DevCockpit:DEV:A"
    assert "Mets directement à jour le roadmap GitHub" in dispatch.prompt_text
    assert "sans confirmation humaine supplémentaire" in dispatch.prompt_text
    assert "Ne lance pas l'analyse ARCH" in dispatch.prompt_text
    assert "ROADMAP_RECONCILE" in dispatch.idempotency_key
    assert len(dispatches.by_key) == 1


def test_stale_developing_branch_prepares_one_same_session_watchdog_follow_up():
    started = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
    now = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    dispatches = DispatchRepository()
    deliveries = Deliveries()

    initial = PromptDispatch.prepare(
        project_id=PROJECT.project_id,
        work_item_id="A",
        role=PromptDispatchRole.DEV,
        prompt_text="Initial A",
        idempotency_key="execution:DevCockpit:A:DEV:INITIAL:v1",
        now=started,
    )
    dispatches.add(initial)
    delivery = PromptDelivery.create(
        dispatch_id=initial.dispatch_id,
        now=started,
    )
    delivery.record_attempt(now=started + timedelta(minutes=1))
    delivery.acknowledge(now=started + timedelta(minutes=2))
    deliveries.save(delivery)

    _, uow_factory = factory(
        dispatches,
        deliveries=deliveries,
    )
    evidence = EvidenceReader(
        {
            "A": ExecutionEvidence(
                default_branch="main",
                branches=(
                    BranchEvidence(
                        name="work/a-load-intervals",
                        sha="stagnant-sha",
                        ahead_by=2,
                        last_activity_at="2026-10-03T07:00:00Z",
                    ),
                ),
            ),
        }
    )
    roadmap = RoadmapReader(v3("A | WORK | READY | #1 | MAIN | A | - | -"))

    first = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=roadmap,
        evidence_reader=evidence,
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        dev_stale_after_seconds=3600,
        clock=lambda: now,
    )
    second = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=roadmap,
        evidence_reader=evidence,
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        dev_stale_after_seconds=3600,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert first.projection.items[0].execution.state is ExecutionState.DEVELOPING
    assert len(first.dispatches) == 1
    watchdog = first.dispatches[0]
    assert watchdog.agent_session == "DevCockpit:DEV:A"
    assert "session DEV de A semble interrompue ou inactive" in watchdog.prompt_text
    assert "work/a-load-intervals" in watchdog.prompt_text
    assert "stagnant-sha" in watchdog.prompt_text
    assert ":DEV:STALE:" in watchdog.idempotency_key
    assert second.dispatches == ()
    assert len(dispatches.by_key) == 2


def test_stale_watchdog_waits_one_hour_after_firefox_ack_even_for_old_branch():
    started = datetime(2026, 10, 3, 9, 40, tzinfo=timezone.utc)
    now = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    dispatches = DispatchRepository()
    deliveries = Deliveries()

    initial = PromptDispatch.prepare(
        project_id=PROJECT.project_id,
        work_item_id="A",
        role=PromptDispatchRole.DEV,
        prompt_text="Initial A",
        idempotency_key="execution:DevCockpit:A:DEV:INITIAL:v1",
        now=started,
    )
    dispatches.add(initial)
    delivery = PromptDelivery.create(dispatch_id=initial.dispatch_id, now=started)
    delivery.record_attempt(now=started + timedelta(minutes=1))
    delivery.acknowledge(now=started + timedelta(minutes=2))
    deliveries.save(delivery)

    _, uow_factory = factory(dispatches, deliveries=deliveries)
    result = evaluate_project_parallel_dev_executions(
        PROJECT,
        roadmap_reader=RoadmapReader(
            v3("A | WORK | READY | #1 | MAIN | A | - | -")
        ),
        evidence_reader=EvidenceReader(
            {
                "A": ExecutionEvidence(
                    default_branch="main",
                    branches=(
                        BranchEvidence(
                            name="work/a-load-intervals",
                            sha="old-sha",
                            ahead_by=2,
                            last_activity_at="2026-10-03T01:00:00Z",
                        ),
                    ),
                ),
            }
        ),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        dev_stale_after_seconds=3600,
        clock=lambda: now,
    )

    assert result.projection.items[0].execution.state is ExecutionState.DEVELOPING
    assert result.dispatches == ()
    assert len(dispatches.by_key) == 1


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



def test_exclusive_surface_allows_only_one_owner_and_explains_conflict():
    project = project_with_locks(
        ("A", (("migration:alembic", ResourceLockMode.EXCLUSIVE),)),
        ("B", (("migration:alembic", ResourceLockMode.EXCLUSIVE),)),
    )
    dispatches, uow_factory = factory()

    result = evaluate_project_parallel_dev_executions(
        project,
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

    by_key = {item.execution.work_item.key: item for item in result.projection.items}
    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A"]
    assert by_key["A"].slot_state is DevExecutionSlotState.ACTIVE
    assert by_key["B"].slot_state is DevExecutionSlotState.WAITING_FOR_RESOURCE_LOCK
    assert by_key["B"].waiting_for_resource_lock
    assert by_key["B"].lock_conflict is not None
    assert by_key["B"].lock_conflict.surface.key == "migration:alembic"
    assert by_key["B"].lock_conflict.holder_work_item_id == "A"
    assert by_key["B"].lock_conflict.requested_mode is ResourceLockMode.EXCLUSIVE
    assert len(dispatches.by_key) == 1


def test_shared_surface_is_compatible_for_parallel_work():
    project = project_with_locks(
        ("A", (("api:contracts", ResourceLockMode.SHARED),)),
        ("B", (("api:contracts", ResourceLockMode.SHARED),)),
    )
    _, uow_factory = factory()

    result = evaluate_project_parallel_dev_executions(
        project,
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

    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A", "B"]
    assert all(item.active for item in result.projection.items)
    assert all(item.lock_conflict is None for item in result.projection.items)


def test_distinct_exclusive_surfaces_do_not_reduce_parallelism():
    project = project_with_locks(
        ("A", (("migration:alembic", ResourceLockMode.EXCLUSIVE),)),
        ("B", (("adr:0010", ResourceLockMode.EXCLUSIVE),)),
    )
    _, uow_factory = factory()

    result = evaluate_project_parallel_dev_executions(
        project,
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

    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A", "B"]


def test_normal_release_makes_same_surface_available_to_next_work_item():
    project = project_with_locks(
        ("A", (("domain:critical", ResourceLockMode.EXCLUSIVE),)),
        ("B", (("domain:critical", ResourceLockMode.EXCLUSIVE),)),
    )
    locks = ResourceLocks()
    dispatches, uow_factory = factory(resource_locks=locks)
    roadmap = RoadmapReader(v3("A | WORK | READY | #1 | MAIN | A | - | -"))

    first = evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )
    assert [dispatch.work_item_id for dispatch in first.dispatches] == ["A"]

    roadmap.body = v3(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | READY | #1 | MAIN | B | - | A",
    )
    second = evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=2,
    )

    records = {
        (lock.work_item_id, lock.surface.key): lock
        for lock in locks.list_for_project(project.project_id)
    }
    assert records[("A", "domain:critical")].state is ResourceLockState.RELEASED
    assert records[("A", "domain:critical")].release_reason == "WORK_ITEM_NO_LONGER_EXECUTABLE"
    assert records[("B", "domain:critical")].state is ResourceLockState.ACTIVE
    assert [dispatch.work_item_id for dispatch in second.dispatches] == ["B"]


def test_expired_lock_is_visible_then_recovered_after_restart_without_redispatch():
    project = project_with_locks(
        ("A", (("file:critical.py", ResourceLockMode.EXCLUSIVE),)),
    )
    locks = ResourceLocks()
    dispatches, uow_factory = factory(resource_locks=locks)
    roadmap = RoadmapReader(v3("A | WORK | READY | #1 | MAIN | A | - | -"))
    started = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    first = evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        resource_lock_lease_seconds=60,
        lease_owner_id="process-one",
        clock=lambda: started,
    )
    assert len(first.dispatches) == 1

    expired_at = started + timedelta(minutes=2)
    before_recovery = read_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        now=expired_at,
    )
    assert before_recovery.items[0].lock_recovery_state == (
        "LEASE_EXPIRED_PENDING_RECONCILIATION"
    )

    recovered = evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
        resource_lock_lease_seconds=60,
        lease_owner_id="process-two",
        clock=lambda: expired_at,
    )

    lock = locks.list_for_owner(project.project_id, "A")[0]
    assert recovered.dispatches == ()
    assert recovered.projection.items[0].active
    assert lock.state is ResourceLockState.ACTIVE
    assert lock.lease_owner_id == "process-two"
    assert lock.version == 3
    assert len(dispatches.by_key) == 1



def test_ci_red_execution_restores_required_lock_before_follow_up():
    project = project_with_locks(
        ("A", (("api:contracts", ResourceLockMode.EXCLUSIVE),)),
        ("B", (("api:contracts", ResourceLockMode.EXCLUSIVE),)),
    )
    locks = ResourceLocks()
    dispatches, uow_factory = factory(resource_locks=locks)
    evidence = EvidenceReader(
        {
            "A": ExecutionEvidence(
                default_branch="main",
                pull_requests=(pr("A", head_sha="aaa"),),
                workflow_runs=(
                    WorkflowRunEvidence(
                        run_id=301,
                        name="CI",
                        status="completed",
                        conclusion="failure",
                        attempt=1,
                        head_sha="aaa",
                        failed_jobs=("backend",),
                    ),
                ),
            ),
        }
    )

    result = evaluate_project_parallel_dev_executions(
        project,
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
    a_lock = locks.list_for_owner(project.project_id, "A")[0]
    assert a_lock.state is ResourceLockState.ACTIVE
    assert a_lock.surface.key == "api:contracts"
    assert by_key["A"].execution.state is ExecutionState.CI_RED
    assert by_key["B"].lock_conflict is not None
    assert by_key["B"].lock_conflict.holder_work_item_id == "A"
    assert len(result.dispatches) == 1
    assert result.dispatches[0].work_item_id == "A"
    assert result.dispatches[0].agent_session == "DevCockpit:DEV:A"
    assert len(dispatches.by_key) == 1


def test_invalid_scheduler_does_not_release_existing_resource_lock():
    project = project_with_locks(
        ("A", (("roadmap:#1", ResourceLockMode.EXCLUSIVE),)),
    )
    locks = ResourceLocks()
    _, uow_factory = factory(resource_locks=locks)
    roadmap = RoadmapReader(v3("A | WORK | READY | #1 | MAIN | A | - | -"))

    evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
    )
    assert locks.list_for_owner(project.project_id, "A")[0].state is ResourceLockState.ACTIVE

    roadmap.body = """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
broken
<!-- /COCKPIT_PIPELINE_V3 -->"""
    result = evaluate_project_parallel_dev_executions(
        project,
        roadmap_reader=roadmap,
        evidence_reader=EvidenceReader(),
        uow_factory=uow_factory,
        max_parallel_dev_executions=1,
    )

    assert result.dispatches == ()
    assert result.projection.scheduler.valid is False
    assert locks.list_for_owner(project.project_id, "A")[0].state is ResourceLockState.ACTIVE


def test_lock_conflict_does_not_waste_capacity_needed_by_independent_candidate():
    project = project_with_locks(
        ("A", (("migration:alembic", ResourceLockMode.EXCLUSIVE),)),
        ("B", (("migration:alembic", ResourceLockMode.EXCLUSIVE),)),
        ("C", (("adr:0010", ResourceLockMode.EXCLUSIVE),)),
    )
    _, uow_factory = factory()

    result = evaluate_project_parallel_dev_executions(
        project,
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

    assert [dispatch.work_item_id for dispatch in result.dispatches] == ["A", "C"]
    by_key = {item.execution.work_item.key: item for item in result.projection.items}
    assert by_key["A"].active
    assert by_key["C"].active
    assert by_key["B"].lock_conflict is not None
