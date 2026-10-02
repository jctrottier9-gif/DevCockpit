import pytest

from app.domain.roadmap import WorkItemStatus, WorkItemType, parse_canonical_pipeline, render_canonical_pipeline


def v3(*rows: str) -> str:
    return "\n".join([
        "<!-- COCKPIT_PIPELINE_V3 -->",
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON",
        *rows,
        "<!-- /COCKPIT_PIPELINE_V3 -->",
    ])


def test_v3_accepts_and_round_trips_environment_gate_type() -> None:
    parsed = parse_canonical_pipeline(v3(
        "ENV-1 | ENVIRONMENT_GATE | DONE | #1 | MAIN | environment gate | - | -",
        "A | WORK | READY | #1 | MAIN | implementation | - | ENV-1",
    ))

    assert parsed.valid
    assert parsed.work_items[0].type is WorkItemType.ENVIRONMENT_GATE
    assert parse_canonical_pipeline(
        render_canonical_pipeline(parsed.work_items, version=3)
    ) == parsed


def test_v3_parses_explicit_dependencies_and_round_trips():
    body = v3(
        "A | WORK | DONE | #1 | MAIN | A | - | -",
        "B | WORK | READY | #1 | MAIN | B | - | A",
        "C | WORK | BLOCKED | #1 | MAIN | C | - | A, B",
    )
    parsed = parse_canonical_pipeline(body)

    assert parsed.valid
    assert parsed.version == 3
    assert parsed.work_items[1].depends_on == ("A",)
    assert parsed.work_items[2].depends_on == ("A", "B")
    assert [(d.work_item_key, d.prerequisite_key) for d in parsed.dependencies] == [
        ("B", "A"), ("C", "A"), ("C", "B")
    ]
    assert parse_canonical_pipeline(
        render_canonical_pipeline(parsed.work_items, version=3)
    ) == parsed


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            v3("A | WORK | READY | #1 | MAIN | A | - | MISSING"),
            "DEPENDENCY_TARGET_MISSING",
        ),
        (
            v3("A | WORK | READY | #1 | MAIN | A | - | A"),
            "SELF_DEPENDENCY",
        ),
        (
            v3(
                "A | WORK | BLOCKED | #1 | MAIN | A | - | B",
                "B | WORK | READY | #1 | MAIN | B | - | A",
            ),
            "DEPENDENCY_CYCLE",
        ),
        (
            v3(
                "A | WORK | BLOCKED | #1 | MAIN | A | - | B",
                "B | WORK | BLOCKED | #1 | MAIN | B | - | C",
                "C | WORK | READY | #1 | MAIN | C | - | A",
            ),
            "DEPENDENCY_CYCLE",
        ),
        (
            v3(
                "A | WORK | DONE | #1 | MAIN | A | - | -",
                "B | WORK | READY | #1 | MAIN | B | - | A, A",
            ),
            "DUPLICATE_DEPENDENCY",
        ),
    ],
)
def test_v3_invalid_dependency_graph_fails_closed(body, code):
    parsed = parse_canonical_pipeline(body)
    assert not parsed.valid
    assert parsed.active_ready_item is None
    assert code in {diagnostic.code for diagnostic in parsed.diagnostics}


def test_v1_and_v2_remain_readable_after_v3_support():
    v1 = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
A | WORK | READY | #1 | MAIN | A
<!-- /COCKPIT_PIPELINE_V1 -->"""
    v2 = """<!-- COCKPIT_PIPELINE_V2 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
A | WORK | READY | #1 | MAIN | A | -
<!-- /COCKPIT_PIPELINE_V2 -->"""
    assert parse_canonical_pipeline(v1).version == 1
    assert parse_canonical_pipeline(v2).version == 2


def test_v3_keeps_superseded_distinct_from_done():
    parsed = parse_canonical_pipeline(v3(
        "OLD | WORK | SUPERSEDED | #1 | MAIN | old | - | -",
        "NEW | WORK | READY | #1 | MAIN | new | OLD | -",
    ))
    assert parsed.valid
    assert parsed.work_items[0].status is WorkItemStatus.SUPERSEDED
