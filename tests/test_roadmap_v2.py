import pytest

from app.domain.roadmap import WorkItemStatus, WorkItemType, parse_canonical_pipeline
from app.domain.roadmap_change import (
    ProposalStatus,
    RoadmapChangeProposal,
    RoadmapChangeProposalRevision,
    body_hash,
    build_preview,
    generate_proposed_body,
    operations_json,
    validate_transition,
)
from app.domain.handoff import utc_now
from uuid import uuid4


def v2(*rows: str) -> str:
    return "\n".join([
        "<!-- COCKPIT_PIPELINE_V2 -->",
        "KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES",
        *rows,
        "<!-- /COCKPIT_PIPELINE_V2 -->",
    ])


def base_roadmap() -> str:
    return """# Roadmap

## État courant

DC-041 est READY.

## Issues de livraison

| Clé | Issue |
|---|---:|
| DC-041 | #11 |

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
ASTRA-041 | ARCHITECTURE_GATE | DONE | #1 | MAIN | architecture gate
DC-041 | WORK | READY | #11 | MAIN | boucle Product Owner
DC-050 | WORK | BLOCKED | #1 | MAIN | scheduler
<!-- /COCKPIT_PIPELINE_V1 -->
"""


def split_operations():
    return [
        {
            "type": "update_current_state_prose",
            "expected": "DC-041 est READY.",
            "replacement": "DC-041A est READY après redécoupage de DC-041.",
        },
        {"type": "supersede_work_item", "key": "DC-041"},
        {
            "type": "add_work_item",
            "key": "DC-041A",
            "item_type": "WORK",
            "status": "READY",
            "parent": "#11",
            "lane": "MAIN",
            "title": "boucle PO propositions révisées et preview",
            "replaces": "DC-041",
            "after": "DC-041",
        },
        {
            "type": "add_work_item",
            "key": "DC-041B",
            "item_type": "WORK",
            "status": "BLOCKED",
            "parent": "#11",
            "lane": "MAIN",
            "title": "application GitHub et réconciliation",
            "replaces": "DC-041",
            "after": "DC-041A",
        },
        {"type": "update_issue_mapping", "key": "DC-041A", "issue_number": 27},
        {"type": "update_issue_mapping", "key": "DC-041B", "issue_number": 28},
    ]


def test_v2_accepts_environment_gate_type() -> None:
    parsed = parse_canonical_pipeline(v2(
        "ENV-1 | ENVIRONMENT_GATE | DONE | #1 | MAIN | environment gate | -",
        "A | WORK | READY | #1 | MAIN | implementation | -",
    ))

    assert parsed.valid
    assert parsed.work_items[0].type is WorkItemType.ENVIRONMENT_GATE
    assert parsed.active_ready_item is not None
    assert parsed.active_ready_item.key == "A"


def test_v2_split_one_old_to_many_is_valid():
    parsed = parse_canonical_pipeline(v2(
        "DC-041 | WORK | SUPERSEDED | #11 | MAIN | parent | -",
        "DC-041A | WORK | READY | #11 | MAIN | A | DC-041",
        "DC-041B | WORK | BLOCKED | #11 | MAIN | B | DC-041",
    ))
    assert parsed.valid
    assert parsed.version == 2
    assert parsed.active_ready_item.key == "DC-041A"
    assert parsed.work_items[0].status is WorkItemStatus.SUPERSEDED
    assert parsed.work_items[1].replaces == "DC-041"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            v2(
                "A | WORK | SUPERSEDED | #1 | MAIN | A | -",
                "B | WORK | READY | #1 | MAIN | B | MISSING",
            ),
            "REPLACEMENT_TARGET_MISSING",
        ),
        (
            v2(
                "A | WORK | READY | #1 | MAIN | A | A",
            ),
            "SELF_REPLACEMENT",
        ),
        (
            v2(
                "A | WORK | SUPERSEDED | #1 | MAIN | A | B",
                "B | WORK | SUPERSEDED | #1 | MAIN | B | A",
                "C | WORK | READY | #1 | MAIN | C | A",
            ),
            "REPLACEMENT_CYCLE",
        ),
        (
            v2(
                "A | WORK | SUPERSEDED | #1 | MAIN | A | -",
                "B | WORK | READY | #1 | MAIN | B | -",
            ),
            "SUPERSEDED_WITHOUT_REPLACEMENT",
        ),
        (
            v2(
                "A | WORK | BLOCKED | #1 | MAIN | A | -",
                "B | WORK | READY | #1 | MAIN | B | A",
            ),
            "REPLACEMENT_TARGET_NOT_SUPERSEDED",
        ),
    ],
)
def test_v2_replacement_rules_fail_closed(body, code):
    parsed = parse_canonical_pipeline(body)
    assert not parsed.valid
    assert code in {item.code for item in parsed.diagnostics}


def test_parser_rejects_mixed_and_unknown_versions():
    mixed = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
A | WORK | READY | #1 | MAIN | A
<!-- /COCKPIT_PIPELINE_V1 -->
""" + v2("B | WORK | READY | #1 | MAIN | B | -")
    assert "MULTIPLE_PIPELINE_VERSIONS" in {
        item.code for item in parse_canonical_pipeline(mixed).diagnostics
    }
    unknown = """<!-- COCKPIT_PIPELINE_V4 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
A | WORK | READY | #1 | MAIN | A
<!-- /COCKPIT_PIPELINE_V4 -->"""
    assert "UNKNOWN_PIPELINE_VERSION" in {
        item.code for item in parse_canonical_pipeline(unknown).diagnostics
    }


def test_operations_generate_v2_and_transition_is_authorized():
    base = base_roadmap()
    operations = split_operations()
    proposed = generate_proposed_body(base, operations)
    parsed = parse_canonical_pipeline(proposed)
    assert parsed.valid
    assert parsed.version == 2
    assert parsed.active_ready_item.key == "DC-041A"
    assert validate_transition(base, proposed, operations) == ()
    assert "| DC-041A | #27 |" in proposed
    assert "| DC-041B | #28 |" in proposed


def test_targeted_prose_fails_closed_when_ambiguous():
    base = base_roadmap().replace(
        "DC-041 est READY.",
        "DC-041 est READY.\nDC-041 est READY.",
    )
    with pytest.raises(ValueError, match="exactly one match"):
        generate_proposed_body(base, [{
            "type": "update_current_state_prose",
            "expected": "DC-041 est READY.",
            "replacement": "changed",
        }])


def test_transition_rejects_false_done_and_missing_mapping():
    base = base_roadmap()
    false_done = [{"type": "set_status", "key": "DC-041", "status": "DONE"}]
    proposed = generate_proposed_body(base, false_done)
    assert "FALSE_DONE" in {
        item.code for item in validate_transition(base, proposed, false_done)
    }

    operations = [
        {"type": "supersede_work_item", "key": "DC-041"},
        {
            "type": "add_work_item",
            "key": "DC-041A",
            "item_type": "WORK",
            "status": "READY",
            "parent": "#11",
            "lane": "MAIN",
            "title": "A",
            "replaces": "DC-041",
            "after": "DC-041",
        },
    ]
    proposed = generate_proposed_body(base, operations)
    assert "MISSING_ISSUE_MAPPING" in {
        item.code for item in validate_transition(base, proposed, operations)
    }


def test_preview_same_revision_is_deterministic_and_new_revision_changes_digest():
    base = base_roadmap()
    operations = split_operations()
    proposed = generate_proposed_body(base, operations)
    proposal = RoadmapChangeProposal(
        proposal_id=uuid4(),
        project_id="DevCockpit",
        repository_full_name="jctrottier9-gif/DevCockpit",
        roadmap_issue_number=1,
        source_decision_id=uuid4(),
        status=ProposalStatus.DRAFT,
        version=1,
        current_revision=1,
        creation_command_id=uuid4(),
        created_by="JC",
        created_at=utc_now(),
    )
    revision = RoadmapChangeProposalRevision(
        proposal_id=proposal.proposal_id,
        revision=1,
        base_body=base,
        base_body_hash=body_hash(base),
        base_updated_at="2026-10-01T00:00:00Z",
        proposed_body=proposed,
        proposed_body_hash=body_hash(proposed),
        operations_json=operations_json(operations),
        generator_version="g1",
        validation_version="v1",
        created_by="JC",
        created_at=utc_now(),
        revision_command_id=uuid4(),
    )
    first = build_preview(proposal, revision)
    second = build_preview(proposal, revision)
    assert first == second
    assert first["old_ready"] == "DC-041"
    assert first["new_ready"] == "DC-041A"
    assert first["superseded_items"] == ["DC-041"]
    assert first["replaces_relationships"] == [
        {"key": "DC-041A", "replaces": "DC-041"},
        {"key": "DC-041B", "replaces": "DC-041"},
    ]

    changed = generate_proposed_body(base, [
        *operations,
        {
            "type": "update_slice_description",
            "expected": "scheduler",
            "replacement": "scheduler déterministe",
        },
    ])
    revision2 = RoadmapChangeProposalRevision(
        proposal_id=proposal.proposal_id,
        revision=2,
        base_body=base,
        base_body_hash=body_hash(base),
        base_updated_at="2026-10-01T00:00:00Z",
        proposed_body=changed,
        proposed_body_hash=body_hash(changed),
        operations_json=operations_json([
            *operations,
            {
                "type": "update_slice_description",
                "expected": "scheduler",
                "replacement": "scheduler déterministe",
            },
        ]),
        generator_version="g1",
        validation_version="v1",
        created_by="JC",
        created_at=utc_now(),
        revision_command_id=uuid4(),
    )
    assert build_preview(proposal, revision2)["preview_digest"] != first["preview_digest"]


def test_v2_multiple_and_incomplete_blocks_fail_closed():
    block = v2("A | WORK | READY | #1 | MAIN | A | -")
    multiple = block + "\n" + v2("B | WORK | BLOCKED | #1 | MAIN | B | -")
    assert "MULTIPLE_CANONICAL_BLOCK" in {
        item.code for item in parse_canonical_pipeline(multiple).diagnostics
    }

    incomplete = """<!-- COCKPIT_PIPELINE_V2 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
A | WORK | READY | #1 | MAIN | A | -
"""
    assert "MISSING_END_MARKER" in {
        item.code for item in parse_canonical_pipeline(incomplete).diagnostics
    }


def test_transition_rejects_historical_deletion_and_key_reuse():
    base = base_roadmap()
    deleted = base.replace(
        "DC-041 | WORK | READY | #11 | MAIN | boucle Product Owner\n",
        "",
    )
    assert "HISTORICAL_WORK_ITEM_REMOVED" in {
        item.code for item in validate_transition(base, deleted, [])
    }

    reused = base.replace(
        "DC-041 | WORK | READY | #11 | MAIN | boucle Product Owner",
        "DC-041 | ARCHITECTURE_GATE | READY | #11 | MAIN | different identity",
    )
    assert "WORK_ITEM_KEY_REUSED" in {
        item.code for item in validate_transition(base, reused, [])
    }


def test_transition_rejects_hidden_ready_promotion_and_gate_skip():
    base = base_roadmap()
    hidden = base.replace(
        "DC-041 | WORK | READY | #11 | MAIN | boucle Product Owner",
        "DC-041 | WORK | BLOCKED | #11 | MAIN | boucle Product Owner",
    ).replace(
        "DC-050 | WORK | BLOCKED | #1 | MAIN | scheduler",
        "DC-050 | WORK | READY | #1 | MAIN | scheduler",
    )
    assert "HIDDEN_READY_PROMOTION" in {
        item.code for item in validate_transition(base, hidden, [])
    }

    gate_base = """# Gate
<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
ASTRA-X | ARCHITECTURE_GATE | BLOCKED | #1 | MAIN | unresolved gate
X | WORK | BLOCKED | #1 | MAIN | candidate
<!-- /COCKPIT_PIPELINE_V1 -->
"""
    operations = [{"type": "set_status", "key": "X", "status": "READY"}]
    gate_proposed = generate_proposed_body(gate_base, operations)
    assert "ARCHITECTURE_GATE_SKIPPED" in {
        item.code for item in validate_transition(gate_base, gate_proposed, operations)
    }


def test_generation_fails_closed_on_invalid_result_and_order_is_deterministic():
    base = base_roadmap()
    with pytest.raises(ValueError, match="generated roadmap body is invalid"):
        generate_proposed_body(
            base,
            [{"type": "set_status", "key": "DC-041", "status": "SUPERSEDED"}],
        )

    operations = [{"type": "set_order", "key": "DC-050", "after": "ASTRA-041"}]
    first = generate_proposed_body(base, operations)
    second = generate_proposed_body(base, operations)
    assert first == second
    assert [item.key for item in parse_canonical_pipeline(first).work_items] == [
        "ASTRA-041",
        "DC-050",
        "DC-041",
    ]
