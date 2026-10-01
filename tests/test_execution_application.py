from app.application.executions import (
    ExecutionSourceError,
    evaluate_project_execution,
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


class FakeUnitOfWork:
    def __init__(self, repository: InMemoryDispatchRepository) -> None:
        self.prompt_dispatches = repository

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def uow_factory(repository: InMemoryDispatchRepository):
    return lambda: FakeUnitOfWork(repository)


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
