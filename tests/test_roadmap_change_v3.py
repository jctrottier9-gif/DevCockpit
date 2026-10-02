from app.domain.roadmap import parse_canonical_pipeline
from app.domain.roadmap_change import generate_proposed_body


def test_roadmap_change_preserves_v3_dependencies():
    base = """# Roadmap
<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
A | WORK | DONE | #1 | MAIN | A | - | -
B | WORK | BLOCKED | #1 | MAIN | B | - | A
<!-- /COCKPIT_PIPELINE_V3 -->"""

    proposed = generate_proposed_body(
        base,
        [{"type": "set_status", "key": "B", "status": "READY"}],
    )
    parsed = parse_canonical_pipeline(proposed)

    assert parsed.valid
    assert parsed.version == 3
    assert parsed.active_ready_item.key == "B"
    assert parsed.active_ready_item.depends_on == ("A",)
