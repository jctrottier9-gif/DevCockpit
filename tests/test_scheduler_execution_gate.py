from app.application.executions import evaluate_project_execution
from app.application.roadmaps import RoadmapIssue
from app.domain.execution import ExecutionEvidence, ExecutionState
from app.domain.project import Project


PROJECT = Project("DevCockpit", "jctrottier9-gif/DevCockpit", 1)


class RoadmapReader:
    def read(self, project):
        return RoadmapIssue(
            project.repository_full_name,
            1,
            """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | BLOCKED | #1 | MAIN | prerequisite | - | -
B | WORK | READY | #1 | MAIN | candidate | - | A
<!-- /COCKPIT_PIPELINE_V3 -->""",
            "now",
        )


class EvidenceReader:
    def __init__(self):
        self.calls = 0

    def read(self, project, work_item):
        self.calls += 1
        return ExecutionEvidence(default_branch="main")


class Dispatches:
    def __init__(self):
        self.by_key = {}

    def get_by_idempotency_key(self, key):
        return self.by_key.get(key)

    def add(self, dispatch):
        self.by_key[dispatch.idempotency_key] = dispatch


class Handoffs:
    def active(self, project_id, work_item_id):
        return None

    def covers(self, project_id, work_item_id, evidence_key):
        return False


class Fences:
    class Snapshot:
        generation = 0
        active_application_id = None

    def snapshot(self, repository_full_name, roadmap_issue_number):
        return self.Snapshot()


class Uow:
    def __init__(self, dispatches):
        self.prompt_dispatches = dispatches
        self.handoffs = Handoffs()
        self.roadmap_target_fences = Fences()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


def test_scheduler_prevents_poller_dispatch_for_unauthorized_ready_item():
    dispatches = Dispatches()
    evidence = EvidenceReader()

    result = evaluate_project_execution(
        PROJECT,
        roadmap_reader=RoadmapReader(),
        evidence_reader=evidence,
        uow_factory=lambda: Uow(dispatches),
    )

    assert result.projection.state is ExecutionState.BLOCKED
    assert {item.code for item in result.projection.diagnostics} == {"WAITING_FOR_DEPENDENCY"}
    assert evidence.calls == 0
    assert result.dispatch is None
    assert dispatches.by_key == {}
