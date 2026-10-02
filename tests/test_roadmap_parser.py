import pytest

from app.domain.roadmap import WorkItemStatus, WorkItemType, parse_canonical_pipeline


def pipeline(*rows: str) -> str:
    return "\n".join(
        [
            "human prose before",
            "<!-- COCKPIT_PIPELINE_V1 -->",
            "KEY | TYPE | STATUS | PARENT | LANE | TITLE",
            *rows,
            "<!-- /COCKPIT_PIPELINE_V1 -->",
            "human prose after",
        ]
    )


def test_parses_devcockpit_like_pipeline_and_selects_ready() -> None:
    result = parse_canonical_pipeline(
        pipeline(
            "DC-001 | WORK | DONE | #1 | MAIN | bootstrap",
            "DC-012 | WORK | DONE | #1 | MAIN | firefox companion",
            "DC-020 | WORK | READY | #1 | MAIN | roadmap parser",
            "DC-021 | WORK | BLOCKED | #1 | MAIN | execution projection",
        )
    )

    assert result.valid is True
    assert result.diagnostics == ()
    assert result.active_ready_item is not None
    assert result.active_ready_item.key == "DC-020"
    assert result.active_ready_item.status is WorkItemStatus.READY


def test_parses_resourceplanner_like_compound_keys_and_architecture_gate() -> None:
    result = parse_canonical_pipeline(
        pipeline(
            "ASTRA-502 | ARCHITECTURE_GATE | DONE | #502 | MAIN | architecture gate",
            "502A | WORK | READY | #502 | MAIN | first slice",
            "502B | WORK | BLOCKED | #502 | MAIN | second slice",
        )
    )

    assert result.valid is True
    assert [item.key for item in result.work_items] == ["ASTRA-502", "502A", "502B"]
    assert result.work_items[0].type is WorkItemType.ARCHITECTURE_GATE


def test_parses_resourceplanner_environment_gates_without_losing_active_ready() -> None:
    result = parse_canonical_pipeline(
        pipeline(
            "ENV-492 | ENVIRONMENT_GATE | DONE | #492 | MAIN | SQLite to SQL Server cutover",
            "ENV-208 | ENVIRONMENT_GATE | DONE | #208 | MAIN | SQL Server authoritative",
            "ASTRA-363 | ARCHITECTURE_GATE | READY | #363 | MAIN | FAT SAT architecture",
            "363A | WORK | BLOCKED | #363 | MAIN | first implementation slice",
        )
    )

    assert result.valid is True
    assert result.diagnostics == ()
    assert result.work_items[0].type is WorkItemType.ENVIRONMENT_GATE
    assert result.work_items[1].type is WorkItemType.ENVIRONMENT_GATE
    assert result.active_ready_item is not None
    assert result.active_ready_item.key == "ASTRA-363"


def test_parses_taskplanner_like_keys_without_replacing_identity() -> None:
    result = parse_canonical_pipeline(
        pipeline(
            "TP-011A | WORK | DONE | #6 | MAIN | domain rules",
            "TP-011B | WORK | DONE | #6 | MAIN | persistence",
            "TP-011C | WORK | READY | #6 | MAIN | application services",
        )
    )

    assert result.valid is True
    assert [item.key for item in result.work_items] == ["TP-011A", "TP-011B", "TP-011C"]
    assert result.active_ready_item is not None
    assert result.active_ready_item.key == "TP-011C"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("no markers", "MISSING_START_MARKER"),
        (
            "<!-- COCKPIT_PIPELINE_V1 -->\nKEY | TYPE | STATUS | PARENT | LANE | TITLE\n",
            "MISSING_END_MARKER",
        ),
        (
            pipeline("A | WORK | READY | #1 | MAIN | one")
            + "\n"
            + pipeline("B | WORK | BLOCKED | #1 | MAIN | two"),
            "MULTIPLE_CANONICAL_BLOCK",
        ),
        (pipeline("A | WORK | READY | #1 | MAIN"), "MALFORMED_ROW"),
        (
            pipeline(
                "A | WORK | DONE | #1 | MAIN | one",
                "A | WORK | READY | #1 | MAIN | duplicate",
            ),
            "DUPLICATE_KEY",
        ),
        (pipeline("A | MAGIC | READY | #1 | MAIN | one"), "UNKNOWN_TYPE"),
        (pipeline("A | WORK | DEVELOPING | #1 | MAIN | one"), "UNKNOWN_STATUS"),
        (pipeline("A | WORK | READY | parent | MAIN | one"), "INVALID_PARENT"),
        (pipeline("A | WORK | READY | #1 | MAIN |   "), "EMPTY_TITLE"),
        (
            pipeline(
                "A | WORK | READY | #1 | MAIN | one",
                "B | WORK | READY | #1 | MAIN | two",
            ),
            "MULTIPLE_MAIN_READY",
        ),
    ],
)
def test_invalid_pipeline_fails_closed(body: str, code: str) -> None:
    result = parse_canonical_pipeline(body)

    assert result.valid is False
    assert result.active_ready_item is None
    assert code in {diagnostic.code for diagnostic in result.diagnostics}


def test_missing_and_extra_header_columns_are_rejected() -> None:
    missing = pipeline("A | WORK | READY | #1 | MAIN | one").replace(
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE",
        "KEY | TYPE | STATUS | PARENT | TITLE",
    )
    extra = pipeline("A | WORK | READY | #1 | MAIN | one").replace(
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE",
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE | EXTRA",
    )

    assert {d.code for d in parse_canonical_pipeline(missing).diagnostics} >= {"MISSING_COLUMN"}
    assert {d.code for d in parse_canonical_pipeline(extra).diagnostics} >= {"EXTRA_COLUMN"}


def test_valid_pipeline_without_ready_is_distinct_from_invalid_pipeline() -> None:
    result = parse_canonical_pipeline(
        pipeline(
            "A | WORK | DONE | #1 | MAIN | one",
            "B | WORK | BLOCKED | #1 | MAIN | two",
        )
    )

    assert result.valid is True
    assert result.active_ready_item is None
