from app.domain.roadmap import parse_canonical_pipeline
from app.domain.scheduler import (
    SchedulerNextAction,
    SchedulerReason,
    SchedulerState,
    derive_scheduler_projection,
)


def v3(*rows: str) -> str:
    return "\n".join([
        "<!-- COCKPIT_PIPELINE_V3 -->",
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON",
        *rows,
        "<!-- /COCKPIT_PIPELINE_V3 -->",
    ])


def project(*rows: str):
    return derive_scheduler_projection(parse_canonical_pipeline(v3(*rows)))


def by_key(projection, key):
    return next(item for item in projection.items if item.work_item.key == key)


def test_simple_chain_unlocks_ready_item_when_dependency_done():
    projection = project(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | READY | #1 | MAIN | B | - | A",
    )
    a = by_key(projection, "A")
    b = by_key(projection, "B")
    assert not a.executable
    assert a.reason is SchedulerReason.ALREADY_DONE
    assert b.executable
    assert b.state is SchedulerState.EXECUTABLE
    assert b.unsatisfied_dependencies == ()
    assert projection.executable_candidates == ("B",)
    assert projection.selected_candidate == "B"


def test_multiple_dependencies_explain_which_one_is_unfinished():
    blocked = project(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | BLOCKED | #1 | MAIN | B | - | -",
        "C | WORK | READY | #1 | MAIN | C | - | A, B",
    )
    c = by_key(blocked, "C")
    assert not c.executable
    assert c.reason is SchedulerReason.WAITING_FOR_DEPENDENCY
    assert c.unsatisfied_dependencies == ("B",)

    eligible = project(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | DONE | #1 | MAIN | B | - | -",
        "C | WORK | READY | #1 | MAIN | C | - | A, B",
    )
    assert by_key(eligible, "C").executable


def test_blocked_item_with_satisfied_dependencies_requires_explicit_ready_promotion():
    projection = project(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | BLOCKED | #1 | MAIN | B | - | A",
    )
    b = by_key(projection, "B")
    assert not b.executable
    assert b.reason is SchedulerReason.CANONICAL_STATUS_BLOCKED
    assert b.next_action is SchedulerNextAction.PROMOTE_READY
    assert b.expected_role == "PO"


def test_superseded_does_not_satisfy_dependency():
    projection = project(
        "OLD | WORK | SUPERSEDED | #1 | MAIN | old | - | -",
        "NEW | WORK | DONE | #1 | MAIN | replacement | OLD | -",
        "C | WORK | READY | #1 | MAIN | C | - | OLD",
    )
    old = by_key(projection, "OLD")
    c = by_key(projection, "C")
    assert old.reason is SchedulerReason.SUPERSEDED
    assert not old.executable
    assert c.unsatisfied_dependencies == ("OLD",)
    assert not c.executable


def test_architecture_gate_routes_expected_role_without_starting_dev():
    projection = project(
        "ARCH | ARCHITECTURE_GATE | READY | #1 | MAIN | gate | - | -",
        "DEV | WORK | BLOCKED | #1 | MAIN | dev | - | ARCH",
    )
    arch = by_key(projection, "ARCH")
    assert arch.executable
    assert arch.expected_role == "ARCH"
    assert arch.next_action is SchedulerNextAction.START_ARCH


def test_same_snapshot_produces_identical_projection():
    pipeline = parse_canonical_pipeline(v3(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | READY | #1 | MAIN | B | - | A",
    ))
    assert derive_scheduler_projection(pipeline) == derive_scheduler_projection(pipeline)


def test_invalid_cycle_is_fail_closed_and_has_no_candidates():
    pipeline = parse_canonical_pipeline(v3(
        "A | WORK | BLOCKED | #1 | MAIN | A | - | B",
        "B | WORK | READY | #1 | MAIN | B | - | A",
    ))
    projection = derive_scheduler_projection(pipeline)
    assert not projection.valid
    assert projection.executable_candidates == ()
    assert projection.selected_candidate is None
    assert "DEPENDENCY_CYCLE" in {d.code for d in projection.diagnostics}
