from uuid import uuid4

from sqlalchemy import inspect, text

from app.config import Settings
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork


def test_upgrade_populated_0005_preserves_all_history_and_adds_writeback_tables(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/from-0005.db")
    upgrade_database(settings, "0005_po_roadmap_proposals")
    engine = build_engine(settings)

    source_dispatch = str(uuid4())
    request_dispatch = str(uuid4())
    delivery_id = str(uuid4())
    response_id = str(uuid4())
    handoff_id = str(uuid4())
    decision_id = str(uuid4())
    proposal_id = str(uuid4())
    creation_command = str(uuid4())
    revision_command = str(uuid4())
    timestamp = "2026-10-02 01:00:00"
    base_body = """<!-- COCKPIT_PIPELINE_V2 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
DC-041 | WORK | SUPERSEDED | #11 | MAIN | parent | -
DC-041A | WORK | DONE | #27 | MAIN | A | DC-041
DC-041B | WORK | READY | #28 | MAIN | B | DC-041
<!-- /COCKPIT_PIPELINE_V2 -->"""
    proposed_body = base_body.replace("DC-041B | WORK | READY", "DC-041B | WORK | BLOCKED")
    import hashlib

    with engine.begin() as connection:
        for dispatch_id, role, session, status, key in [
            (source_dispatch, "DEV", "DevCockpit:DEV:DC-041B", "CANCELLED", "source"),
            (request_dispatch, "PO", "DevCockpit:PO:DC-041B", "PREPARED", "po"),
        ]:
            connection.execute(text("""
                INSERT INTO prompt_dispatches (
                    dispatch_id, project_id, work_item_id, role, agent_session,
                    prompt_text, status, idempotency_key, created_at, updated_at
                ) VALUES (
                    :dispatch_id, 'DevCockpit', 'DC-041B', :role, :session,
                    'historical prompt', :status, :key, :ts, :ts
                )
            """), {
                "dispatch_id": dispatch_id,
                "role": role,
                "session": session,
                "status": status,
                "key": key,
                "ts": timestamp,
            })
        connection.execute(text("""
            INSERT INTO prompt_deliveries (
                delivery_id, dispatch_id, status, attempt_count,
                last_attempt_at, acknowledged_at, created_at, updated_at
            ) VALUES (:id, :dispatch, 'ACKNOWLEDGED', 1, :ts, :ts, :ts, :ts)
        """), {"id": delivery_id, "dispatch": request_dispatch, "ts": timestamp})
        connection.execute(text("""
            INSERT INTO imported_chatgpt_responses (
                response_id, delivery_id, text, imported_at
            ) VALUES (:id, :delivery, 'PO response', :ts)
        """), {"id": response_id, "delivery": delivery_id, "ts": timestamp})
        connection.execute(text("""
            INSERT INTO handoffs (
                handoff_id, project_id, work_item_id, source_dispatch_id,
                source_response_id, question, context, context_snapshot,
                request_dispatch_id, resume_dispatch_id, creation_command_id,
                created_by, created_at, target_role, purpose, status, version,
                covered_github_evidence, resume_held_reason,
                predecessor_handoff_id, context_decision_id,
                cancelled_by, cancelled_at, cancel_reason, cancellation_command_id
            ) VALUES (
                :id, 'DevCockpit', 'DC-041B', :source, NULL,
                'review', 'context', 'snapshot', :request, NULL, :creation,
                'JC', :ts, 'PO', 'ROADMAP_REVIEW', 'DECIDED', 2,
                NULL, 'HOLD_FOR_AUTHORIZATION', NULL, NULL,
                NULL, NULL, NULL, NULL
            )
        """), {
            "id": handoff_id,
            "source": source_dispatch,
            "request": request_dispatch,
            "creation": str(uuid4()),
            "ts": timestamp,
        })
        connection.execute(text("""
            INSERT INTO decisions (
                decision_id, source_handoff_id, source_response_id,
                summary, effect, accepted_by, accepted_at,
                acceptance_command_id, decision_type
            ) VALUES (
                :id, :handoff, :response, 'scope decision',
                'HOLD_FOR_AUTHORIZATION', 'JC', :ts, :command, 'SCOPE_DECISION'
            )
        """), {
            "id": decision_id,
            "handoff": handoff_id,
            "response": response_id,
            "ts": timestamp,
            "command": str(uuid4()),
        })
        connection.execute(text("""
            INSERT INTO roadmap_change_proposals (
                proposal_id, project_id, repository_full_name, roadmap_issue_number,
                source_decision_id, status, version, current_revision,
                creation_command_id, created_by, created_at,
                cancelled_by, cancelled_at, cancellation_command_id
            ) VALUES (
                :id, 'DevCockpit', 'jctrottier9-gif/DevCockpit', 1,
                :decision, 'DRAFT', 1, 1, :command, 'JC', :ts,
                NULL, NULL, NULL
            )
        """), {
            "id": proposal_id,
            "decision": decision_id,
            "command": creation_command,
            "ts": timestamp,
        })
        connection.execute(text("""
            INSERT INTO roadmap_change_proposal_revisions (
                proposal_id, revision, base_body, base_body_hash, base_updated_at,
                proposed_body, proposed_body_hash, operations_json,
                generator_version, validation_version, created_by, created_at,
                revision_command_id
            ) VALUES (
                :proposal, 1, :base, :base_hash, '2026-10-02T01:00:00Z',
                :proposed, :proposed_hash, '[]',
                'dc041a-generator-v1', 'dc041a-validation-v1', 'JC', :ts, :command
            )
        """), {
            "proposal": proposal_id,
            "base": base_body,
            "base_hash": hashlib.sha256(base_body.encode()).hexdigest(),
            "proposed": proposed_body,
            "proposed_hash": hashlib.sha256(proposed_body.encode()).hexdigest(),
            "ts": timestamp,
            "command": revision_command,
        })

    upgrade_database(settings)

    with engine.connect() as connection:
        proposal = connection.execute(text("""
            SELECT source_decision_id, status, version, current_revision,
                   confirmation_command_id, confirmed_revision, applied_at
            FROM roadmap_change_proposals WHERE proposal_id=:id
        """), {"id": proposal_id}).mappings().one()
        assert proposal["source_decision_id"] == decision_id
        assert proposal["status"] == "DRAFT"
        assert proposal["version"] == 1
        assert proposal["current_revision"] == 1
        assert proposal["confirmation_command_id"] is None
        assert proposal["confirmed_revision"] is None
        assert proposal["applied_at"] is None

        revision = connection.execute(text("""
            SELECT revision, base_body, proposed_body, revision_command_id
            FROM roadmap_change_proposal_revisions WHERE proposal_id=:id
        """), {"id": proposal_id}).mappings().one()
        assert revision["revision"] == 1
        assert revision["base_body"] == base_body
        assert revision["proposed_body"] == proposed_body
        assert revision["revision_command_id"] == revision_command

        assert connection.execute(
            text("SELECT text FROM imported_chatgpt_responses WHERE response_id=:id"),
            {"id": response_id},
        ).scalar_one() == "PO response"
        assert connection.execute(
            text("SELECT summary FROM decisions WHERE decision_id=:id"),
            {"id": decision_id},
        ).scalar_one() == "scope decision"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0010_pr_finalization_attempts"

    inspector = inspect(engine)
    assert {
        "roadmap_change_proposals",
        "roadmap_change_proposal_revisions",
        "roadmap_writeback_authorizations",
        "roadmap_change_applications",
        "roadmap_change_application_attempts",
        "roadmap_target_fences",
    } <= set(inspector.get_table_names())
    assert {"ix_roadmap_applications_target_status"} <= {
        item["name"] for item in inspector.get_indexes("roadmap_change_applications")
    }
    engine.dispose()

    restarted_engine = build_engine(settings)
    restarted_factory = build_session_factory(restarted_engine)
    with SqlAlchemyUnitOfWork(restarted_factory) as uow:
        persisted = uow.roadmap_change_proposals.get(proposal_id)
        persisted_revision = uow.roadmap_change_proposal_revisions.get(proposal_id, 1)
        assert persisted is not None
        assert persisted.status.value == "DRAFT"
        assert persisted_revision is not None
        assert persisted_revision.proposed_body == proposed_body
    restarted_engine.dispose()
