from uuid import uuid4

from sqlalchemy import inspect, text

from app.config import Settings
from app.infrastructure.database import build_engine, upgrade_database


def test_upgrade_populated_0004_preserves_handoff_decision_and_history(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/from-0004.db")
    upgrade_database(settings, "0004_handoffs_decisions")
    engine = build_engine(settings)

    source_dispatch = str(uuid4())
    request_dispatch = str(uuid4())
    delivery_id = str(uuid4())
    response_id = str(uuid4())
    handoff_id = str(uuid4())
    decision_id = str(uuid4())
    creation_command_id = str(uuid4())
    acceptance_command_id = str(uuid4())
    timestamp = "2026-10-01 12:00:00"

    with engine.begin() as connection:
        for dispatch_id, role, session, status, key in [
            (
                source_dispatch,
                "DEV",
                "DevCockpit:DEV:DC-040",
                "CANCELLED",
                "source-key",
            ),
            (
                request_dispatch,
                "ARCH",
                "DevCockpit:ARCH:DC-040",
                "PREPARED",
                "request-key",
            ),
        ]:
            connection.execute(text("""
                INSERT INTO prompt_dispatches (
                    dispatch_id, project_id, work_item_id, role, agent_session,
                    prompt_text, status, idempotency_key, created_at, updated_at
                ) VALUES (
                    :dispatch_id, 'DevCockpit', 'DC-040', :role, :session,
                    'historical prompt', :status, :key, :created_at, :updated_at
                )
            """), {
                "dispatch_id": dispatch_id,
                "role": role,
                "session": session,
                "status": status,
                "key": key,
                "created_at": timestamp,
                "updated_at": timestamp,
            })

        connection.execute(text("""
            INSERT INTO prompt_deliveries (
                delivery_id, dispatch_id, status, attempt_count,
                last_attempt_at, acknowledged_at, created_at, updated_at
            ) VALUES (
                :delivery_id, :dispatch_id, 'ACKNOWLEDGED', 1,
                :ts, :ts, :ts, :ts
            )
        """), {"delivery_id": delivery_id, "dispatch_id": request_dispatch, "ts": timestamp})
        connection.execute(text("""
            INSERT INTO imported_chatgpt_responses (
                response_id, delivery_id, text, imported_at
            ) VALUES (:response_id, :delivery_id, 'historical ARCH answer', :ts)
        """), {"response_id": response_id, "delivery_id": delivery_id, "ts": timestamp})
        connection.execute(text("""
            INSERT INTO handoffs (
                handoff_id, project_id, work_item_id, source_dispatch_id,
                source_response_id, question, context, context_snapshot,
                request_dispatch_id, resume_dispatch_id, creation_command_id,
                created_by, created_at, target_role, purpose, status, version,
                covered_github_evidence, resume_held_reason,
                cancelled_by, cancelled_at, cancel_reason, cancellation_command_id
            ) VALUES (
                :handoff_id, 'DevCockpit', 'DC-040', :source_dispatch_id,
                NULL, 'question', 'context', 'snapshot',
                :request_dispatch_id, NULL, :creation_command_id,
                'JC', :ts, 'ARCH', 'TECHNICAL_GUIDANCE', 'DECIDED', 2,
                NULL, 'HOLD_FOR_AUTHORIZATION',
                NULL, NULL, NULL, NULL
            )
        """), {
            "handoff_id": handoff_id,
            "source_dispatch_id": source_dispatch,
            "request_dispatch_id": request_dispatch,
            "creation_command_id": creation_command_id,
            "ts": timestamp,
        })
        connection.execute(text("""
            INSERT INTO decisions (
                decision_id, source_handoff_id, source_response_id,
                summary, effect, accepted_by, accepted_at,
                acceptance_command_id, decision_type
            ) VALUES (
                :decision_id, :handoff_id, :response_id,
                'historical accepted conclusion', 'HOLD_FOR_AUTHORIZATION',
                'JC', :ts, :acceptance_command_id, 'ARCHITECTURE_GUIDANCE'
            )
        """), {
            "decision_id": decision_id,
            "handoff_id": handoff_id,
            "response_id": response_id,
            "acceptance_command_id": acceptance_command_id,
            "ts": timestamp,
        })

    upgrade_database(settings)

    with engine.connect() as connection:
        handoff = connection.execute(text("""
            SELECT status, version, target_role, purpose, predecessor_handoff_id,
                   context_decision_id, resume_held_reason
            FROM handoffs WHERE handoff_id=:id
        """), {"id": handoff_id}).mappings().one()
        assert handoff["status"] == "DECIDED"
        assert handoff["version"] == 2
        assert handoff["target_role"] == "ARCH"
        assert handoff["purpose"] == "TECHNICAL_GUIDANCE"
        assert handoff["predecessor_handoff_id"] is None
        assert handoff["context_decision_id"] is None
        assert handoff["resume_held_reason"] == "HOLD_FOR_AUTHORIZATION"

        decision = connection.execute(text("""
            SELECT source_response_id, summary, effect, decision_type
            FROM decisions WHERE decision_id=:id
        """), {"id": decision_id}).mappings().one()
        assert decision["source_response_id"] == response_id
        assert decision["summary"] == "historical accepted conclusion"
        assert decision["effect"] == "HOLD_FOR_AUTHORIZATION"
        assert decision["decision_type"] == "ARCHITECTURE_GUIDANCE"

        assert connection.execute(
            text("SELECT text FROM imported_chatgpt_responses WHERE response_id=:id"),
            {"id": response_id},
        ).scalar_one() == "historical ARCH answer"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0005_po_roadmap_proposals"

    inspector = inspect(engine)
    assert {"roadmap_change_proposals", "roadmap_change_proposal_revisions"} <= set(
        inspector.get_table_names()
    )
    assert {"uq_handoffs_active", "ix_handoffs_project_work_status"} <= {
        item["name"] for item in inspector.get_indexes("handoffs")
    }
    engine.dispose()
