from app.application.executions import (
    ExecutionSourceError,
    evaluate_project_execution,
)
from app.application.roadmaps import RoadmapIssue
from app.domain.execution import (
    ExecutionEvidence,
    ExecutionState,
    NextAction,
    PullRequestEvidence,
    WorkflowRunEvidence,
)
from app.domain.project import Project


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)
ROADMAP = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
DC-020 | WORK | DONE | #1 | MAIN | projets GitHub et parser roadmap canonique
DC-021 | WORK | READY | #1 | MAIN | projection exécution et suivi CI
DC-030 | WORK | BLOCKED | #1 | MAIN | retour explicite réponse ChatGPT
<!-- /COCKPIT_PIPELINE_V1 -->"""


class RoadmapReader:
    def read(self, project: Project) -> RoadmapIssue:
        return RoadmapIssue(
            project.repository_full_name,
            project.roadmap_issue_number,
            ROADMAP,
            "2026-10-01T12:00:00Z",
        )


class EvidenceReader:
    def __init__(self, evidence: ExecutionEvidence) -> None:
        self.evidence = evidence

    def read(self, project, work_item) -> ExecutionEvidence:
        return self.evidence


class OfflineEvidenceReader:
    def read(self, project, work_item) -> ExecutionEvidence:
        raise ExecutionSourceError("offline")


class InMemoryDispatchRepository:
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


class EmptyHandoffs:
    def active(self, project_id, work_item_id):
        return None

    def covers(self, project_id, work_item_id, evidence_key):
        return False


class EmptyFenceRepository:
    def __init__(self):
        self.generation = 0
        self.active_application_id = None

    class Snapshot:
        def __init__(self, generation, active_application_id):
            self.generation = generation
            self.active_application_id = active_application_id

    def snapshot(self, repository_full_name, roadmap_issue_number):
        return self.Snapshot(self.generation, self.active_application_id)


class FakeUnitOfWork:
    def __init__(self, repository: InMemoryDispatchRepository, fences: EmptyFenceRepository) -> None:
        self.prompt_dispatches = repository
        self.handoffs = EmptyHandoffs()
        self.roadmap_target_fences = fences

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def uow_factory(repository: InMemoryDispatchRepository, fences=None):
    shared_fences = fences or EmptyFenceRepository()
    return lambda: FakeUnitOfWork(repository, shared_fences)


def open_pr() -> PullRequestEvidence:
    return PullRequestEvidence(
        number=22,
        title="DC-021 — Projection d'exécution, CI et follow-up DEV",
        body="",
        branch="dc-021-execution-ci",
        head_sha="abc123",
        state="open",
        merged=False,
        mergeable=False,
        url="https://github.example/pr/22",
        updated_at="2026-10-01T12:00:00Z",
    )


def green_run() -> WorkflowRunEvidence:
    return WorkflowRunEvidence(
        run_id=456,
        name="CI",
        status="completed",
        conclusion="success",
        attempt=1,
        head_sha="abc123",
        url="https://github.example/actions/456",
    )


def red_run(*, attempt: int = 1) -> WorkflowRunEvidence:
    return WorkflowRunEvidence(
        run_id=123,
        name="CI",
        status="completed",
        conclusion="failure",
        attempt=attempt,
        head_sha="abc123",
        url="https://github.example/actions/123",
        failed_jobs=("backend / pytest", "extension / test"),
    )


def test_ready_evaluation_creates_one_idempotent_initial_dispatch() -> None:
    repository = InMemoryDispatchRepository()
    kwargs = {
        "roadmap_reader": RoadmapReader(),
        "evidence_reader": EvidenceReader(ExecutionEvidence(default_branch="main")),
        "uow_factory": uow_factory(repository),
    }

    first = evaluate_project_execution(PROJECT, **kwargs)
    second = evaluate_project_execution(PROJECT, **kwargs)

    assert first.projection.state is ExecutionState.READY
    assert first.dispatch is not None
    assert second.dispatch is not None
    assert first.dispatch.dispatch_id == second.dispatch.dispatch_id
    assert len(repository.by_key) == 1
    assert first.dispatch.agent_session == "DevCockpit:DEV:DC-021"
    assert "AGENTS.md" in first.dispatch.prompt_text
    assert "COCKPIT_PIPELINE_V1" in first.dispatch.prompt_text
    assert "Ne commence pas la tranche suivante" in first.dispatch.prompt_text


def test_ci_red_repoll_reuses_dispatch_and_same_agent_session() -> None:
    repository = InMemoryDispatchRepository()
    evidence = ExecutionEvidence(
        default_branch="main",
        pull_requests=(open_pr(),),
        workflow_runs=(red_run(),),
    )
    kwargs = {
        "roadmap_reader": RoadmapReader(),
        "evidence_reader": EvidenceReader(evidence),
        "uow_factory": uow_factory(repository),
    }

    first = evaluate_project_execution(PROJECT, **kwargs)
    second = evaluate_project_execution(PROJECT, **kwargs)

    assert first.projection.state is ExecutionState.CI_RED
    assert first.dispatch is not None
    assert second.dispatch is not None
    assert first.dispatch.dispatch_id == second.dispatch.dispatch_id
    assert first.dispatch.agent_session == "DevCockpit:DEV:DC-021"
    assert first.dispatch.agent_session == second.dispatch.agent_session
    assert len(repository.by_key) == 1
    assert "backend / pytest" in first.dispatch.prompt_text
    assert "head SHA abc123" in first.dispatch.prompt_text


def test_distinct_ci_attempt_can_create_a_new_follow_up_in_same_session() -> None:
    repository = InMemoryDispatchRepository()
    first = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=EvidenceReader(
            ExecutionEvidence(
                default_branch="main",
                pull_requests=(open_pr(),),
                workflow_runs=(red_run(attempt=1),),
            )
        ),
        uow_factory=uow_factory(repository),
    )
    second = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=EvidenceReader(
            ExecutionEvidence(
                default_branch="main",
                pull_requests=(open_pr(),),
                workflow_runs=(red_run(attempt=2),),
            )
        ),
        uow_factory=uow_factory(repository),
    )

    assert first.dispatch is not None
    assert second.dispatch is not None
    assert first.dispatch.dispatch_id != second.dispatch.dispatch_id
    assert first.dispatch.agent_session == second.dispatch.agent_session
    assert len(repository.by_key) == 2


def test_ready_to_merge_no_longer_creates_a_dev_merge_prompt() -> None:
    repository = InMemoryDispatchRepository()
    evidence = ExecutionEvidence(
        default_branch="main",
        pull_requests=(
            PullRequestEvidence(
                number=22,
                title="DC-021 — Projection d'exécution, CI et follow-up DEV",
                body="",
                branch="dc-021-execution-ci",
                head_sha="abc123",
                state="open",
                merged=False,
                mergeable=True,
                auto_merge_enabled=False,
                url="https://github.example/pr/22",
                updated_at="2026-10-01T12:00:00Z",
            ),
        ),
        workflow_runs=(green_run(),),
    )

    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=EvidenceReader(evidence),
        uow_factory=uow_factory(repository),
    )

    assert result.projection.state is ExecutionState.READY_TO_MERGE
    assert result.projection.next_action is NextAction.MERGE_PR
    assert result.dispatch is None
    assert repository.by_key == {}


def test_ready_to_merge_with_auto_merge_armed_creates_no_dispatch() -> None:
    repository = InMemoryDispatchRepository()
    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=EvidenceReader(
            ExecutionEvidence(
                default_branch="main",
                pull_requests=(
                    PullRequestEvidence(
                        number=22,
                        title="DC-021 — Projection d'exécution, CI et follow-up DEV",
                        body="",
                        branch="dc-021-execution-ci",
                        head_sha="abc123",
                        state="open",
                        merged=False,
                        mergeable=True,
                        auto_merge_enabled=True,
                        url="https://github.example/pr/22",
                    ),
                ),
                workflow_runs=(green_run(),),
            )
        ),
        uow_factory=uow_factory(repository),
    )

    assert result.projection.state is ExecutionState.READY_TO_MERGE
    assert result.dispatch is None
    assert repository.by_key == {}


def test_github_source_failure_fails_closed_without_dispatch() -> None:
    repository = InMemoryDispatchRepository()

    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=OfflineEvidenceReader(),
        uow_factory=uow_factory(repository),
    )

    assert result.projection.state is ExecutionState.BLOCKED
    assert {item.code for item in result.projection.diagnostics} == {"GITHUB_UNAVAILABLE"}
    assert result.dispatch is None
    assert repository.by_key == {}


def test_poller_generation_fence_rejects_projection_read_before_writeback():
    repository = InMemoryDispatchRepository()
    fences = EmptyFenceRepository()

    class RacingEvidenceReader(EvidenceReader):
        def read(self, project, work_item):
            # Simulate a roadmap application that starts and reaches a terminal
            # state while this poller is reading old GitHub evidence.
            fences.generation += 1
            fences.active_application_id = None
            return super().read(project, work_item)

    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=RacingEvidenceReader(ExecutionEvidence(default_branch="main")),
        uow_factory=uow_factory(repository, fences),
    )

    assert result.projection.state is ExecutionState.READY
    assert result.dispatch is None
    assert repository.by_key == {}


def test_poller_is_inhibited_while_roadmap_application_is_active():
    repository = InMemoryDispatchRepository()
    fences = EmptyFenceRepository()
    fences.generation = 4
    fences.active_application_id = "active"

    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=EvidenceReader(ExecutionEvidence(default_branch="main")),
        uow_factory=uow_factory(repository, fences),
    )

    assert result.dispatch is None
    assert repository.by_key == {}
