"""PO handoffs and roadmap change proposals.

Revision ID: 0005_po_roadmap_proposals
Revises: 0004_handoffs_decisions
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_po_roadmap_proposals"
down_revision: Union[str, None] = "0004_handoffs_decisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HANDOFF_OLD_COLUMNS = (
    "handoff_id, project_id, work_item_id, source_dispatch_id, source_response_id, "
    "question, context, context_snapshot, request_dispatch_id, resume_dispatch_id, "
    "creation_command_id, created_by, created_at, target_role, purpose, status, version, "
    "covered_github_evidence, resume_held_reason, cancelled_by, cancelled_at, "
    "cancel_reason, cancellation_command_id"
)
_DECISION_COLUMNS = (
    "decision_id, source_handoff_id, source_response_id, summary, effect, accepted_by, "
    "accepted_at, acceptance_command_id, decision_type"
)


def _create_handoffs() -> None:
    op.create_table(
        "handoffs",
        sa.Column("handoff_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("source_dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("source_response_id", sa.String(length=36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=False),
        sa.Column("request_dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("resume_dispatch_id", sa.String(length=36), nullable=True),
        sa.Column("creation_command_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_role", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("covered_github_evidence", sa.Text(), nullable=True),
        sa.Column("resume_held_reason", sa.Text(), nullable=True),
        sa.Column("predecessor_handoff_id", sa.String(length=36), nullable=True),
        sa.Column("context_decision_id", sa.String(length=36), nullable=True),
        sa.Column("cancelled_by", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_command_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL AND length(trim(cancelled_by)) > 0 "
            "AND length(trim(cancel_reason)) > 0 AND cancellation_command_id IS NOT NULL) "
            "OR (status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL AND cancellation_command_id IS NULL)",
            name="ck_handoff_cancel",
        ),
        sa.CheckConstraint(
            "(status = 'RESUME_PREPARED' AND resume_dispatch_id IS NOT NULL) "
            "OR (status != 'RESUME_PREPARED' AND resume_dispatch_id IS NULL)",
            name="ck_handoff_resume",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','DECIDED','RESUME_PREPARED','TRANSFERRED','RESOLVED_NO_RESUME','CANCELLED')",
            name="ck_handoff_status",
        ),
        sa.CheckConstraint(
            "(target_role = 'ARCH' AND purpose = 'TECHNICAL_GUIDANCE') OR "
            "(target_role = 'PO' AND purpose IN ('PRODUCT_CLARIFICATION','ROADMAP_REVIEW'))",
            name="ck_handoff_role",
        ),
        sa.CheckConstraint(
            "length(trim(question)) > 0 AND length(trim(context)) > 0 "
            "AND length(trim(context_snapshot)) > 0 AND length(trim(created_by)) > 0",
            name="ck_handoff_text",
        ),
        sa.CheckConstraint("version >= 1", name="ck_handoff_version"),
        sa.CheckConstraint(
            "predecessor_handoff_id IS NULL OR predecessor_handoff_id <> handoff_id",
            name="ck_handoff_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["request_dispatch_id"], ["prompt_dispatches.dispatch_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resume_dispatch_id"], ["prompt_dispatches.dispatch_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_dispatch_id"], ["prompt_dispatches.dispatch_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_response_id"], ["imported_chatgpt_responses.response_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_handoff_id"], ["handoffs.handoff_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["context_decision_id"], ["decisions.decision_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("handoff_id"),
        sa.UniqueConstraint("cancellation_command_id"),
        sa.UniqueConstraint("creation_command_id"),
        sa.UniqueConstraint("request_dispatch_id"),
        sa.UniqueConstraint("resume_dispatch_id"),
    )
    op.create_index(
        "ix_handoffs_project_work_status",
        "handoffs",
        ["project_id", "work_item_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_handoffs_active",
        "handoffs",
        ["project_id", "work_item_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('OPEN','DECIDED')"),
    )


def _create_decisions(*, extended: bool) -> None:
    decision_check = (
        "decision_type IN ('ARCHITECTURE_GUIDANCE','PRODUCT_CLARIFICATION','SCOPE_DECISION')"
        if extended else "decision_type = 'ARCHITECTURE_GUIDANCE'"
    )
    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("source_handoff_id", sa.String(length=36), nullable=False),
        sa.Column("source_response_id", sa.String(length=36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("accepted_by", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acceptance_command_id", sa.String(length=36), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "effect IN ('CONTINUE_IN_SCOPE','HOLD_FOR_AUTHORIZATION')",
            name="ck_decision_effect",
        ),
        sa.CheckConstraint(decision_check, name="ck_decision_type"),
        sa.CheckConstraint(
            "length(trim(summary)) > 0 AND length(trim(accepted_by)) > 0",
            name="ck_decision_text",
        ),
        sa.ForeignKeyConstraint(
            ["source_handoff_id"], ["handoffs.handoff_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_response_id"], ["imported_chatgpt_responses.response_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("acceptance_command_id"),
        sa.UniqueConstraint("source_handoff_id"),
    )


def upgrade() -> None:
    # Preserve 0004 Decisions before removing the only inbound FK that prevents
    # controlled reconstruction of the Handoff table on SQLite.
    op.create_table(
        "_dc041a_decisions_backup",
        sa.Column("decision_id", sa.String(length=36), primary_key=True),
        sa.Column("source_handoff_id", sa.String(length=36), nullable=False),
        sa.Column("source_response_id", sa.String(length=36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("accepted_by", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acceptance_command_id", sa.String(length=36), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
    )
    op.execute(sa.text(
        f"INSERT INTO _dc041a_decisions_backup ({_DECISION_COLUMNS}) "
        f"SELECT {_DECISION_COLUMNS} FROM decisions"
    ))
    op.drop_table("decisions")

    op.drop_index("uq_handoffs_active", table_name="handoffs")
    op.drop_index("ix_handoffs_project_work_status", table_name="handoffs")
    op.rename_table("handoffs", "_dc041a_handoffs_backup")

    _create_handoffs()
    _create_decisions(extended=True)

    op.execute(sa.text(
        f"INSERT INTO handoffs ({_HANDOFF_OLD_COLUMNS}, predecessor_handoff_id, context_decision_id) "
        f"SELECT {_HANDOFF_OLD_COLUMNS}, NULL, NULL FROM _dc041a_handoffs_backup"
    ))
    op.execute(sa.text(
        f"INSERT INTO decisions ({_DECISION_COLUMNS}) "
        f"SELECT {_DECISION_COLUMNS} FROM _dc041a_decisions_backup"
    ))
    op.drop_table("_dc041a_handoffs_backup")
    op.drop_table("_dc041a_decisions_backup")

    op.create_table(
        "roadmap_change_proposals",
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("repository_full_name", sa.String(length=300), nullable=False),
        sa.Column("roadmap_issue_number", sa.Integer(), nullable=False),
        sa.Column("source_decision_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("creation_command_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_by", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_command_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint("status IN ('DRAFT','CANCELLED')", name="ck_roadmap_proposal_status"),
        sa.CheckConstraint(
            "version >= 1 AND current_revision >= 1", name="ck_roadmap_proposal_version"
        ),
        sa.CheckConstraint(
            "roadmap_issue_number >= 1 AND length(trim(project_id)) > 0 "
            "AND length(trim(repository_full_name)) > 0 AND length(trim(created_by)) > 0",
            name="ck_roadmap_proposal_identity",
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED' AND cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL "
            "AND cancellation_command_id IS NOT NULL AND length(trim(cancelled_by)) > 0) "
            "OR (status != 'CANCELLED' AND cancelled_by IS NULL AND cancelled_at IS NULL "
            "AND cancellation_command_id IS NULL)",
            name="ck_roadmap_proposal_cancel",
        ),
        sa.ForeignKeyConstraint(
            ["source_decision_id"], ["decisions.decision_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("proposal_id"),
        sa.UniqueConstraint("creation_command_id"),
        sa.UniqueConstraint("cancellation_command_id"),
    )
    op.create_table(
        "roadmap_change_proposal_revisions",
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("base_body", sa.Text(), nullable=False),
        sa.Column("base_body_hash", sa.String(length=64), nullable=False),
        sa.Column("base_updated_at", sa.Text(), nullable=True),
        sa.Column("proposed_body", sa.Text(), nullable=False),
        sa.Column("proposed_body_hash", sa.String(length=64), nullable=False),
        sa.Column("operations_json", sa.Text(), nullable=False),
        sa.Column("generator_version", sa.String(length=80), nullable=False),
        sa.Column("validation_version", sa.String(length=80), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_command_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_roadmap_proposal_revision_number"),
        sa.CheckConstraint(
            "length(base_body_hash) = 64 AND length(proposed_body_hash) = 64",
            name="ck_roadmap_proposal_revision_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["roadmap_change_proposals.proposal_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("proposal_id", "revision"),
        sa.UniqueConstraint(
            "revision_command_id", name="uq_roadmap_proposal_revision_command"
        ),
    )


def downgrade() -> None:
    op.drop_table("roadmap_change_proposal_revisions")
    op.drop_table("roadmap_change_proposals")

    op.create_table(
        "_dc041a_decisions_backup",
        sa.Column("decision_id", sa.String(length=36), primary_key=True),
        sa.Column("source_handoff_id", sa.String(length=36), nullable=False),
        sa.Column("source_response_id", sa.String(length=36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("accepted_by", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acceptance_command_id", sa.String(length=36), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
    )
    op.execute(sa.text(
        f"INSERT INTO _dc041a_decisions_backup ({_DECISION_COLUMNS}) "
        f"SELECT {_DECISION_COLUMNS} FROM decisions "
        "WHERE decision_type = 'ARCHITECTURE_GUIDANCE'"
    ))
    op.drop_table("decisions")

    op.create_table(
        "_dc041a_handoffs_backup",
        sa.Column("handoff_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("source_dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("source_response_id", sa.String(length=36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=False),
        sa.Column("request_dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("resume_dispatch_id", sa.String(length=36), nullable=True),
        sa.Column("creation_command_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_role", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("covered_github_evidence", sa.Text(), nullable=True),
        sa.Column("resume_held_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_command_id", sa.String(length=36), nullable=True),
    )
    op.execute(sa.text(
        f"INSERT INTO _dc041a_handoffs_backup ({_HANDOFF_OLD_COLUMNS}) "
        f"SELECT {_HANDOFF_OLD_COLUMNS} FROM handoffs "
        "WHERE target_role = 'ARCH' AND purpose = 'TECHNICAL_GUIDANCE' "
        "AND status IN ('OPEN','DECIDED','RESUME_PREPARED','CANCELLED')"
    ))
    op.drop_index("uq_handoffs_active", table_name="handoffs")
    op.drop_index("ix_handoffs_project_work_status", table_name="handoffs")
    op.drop_table("handoffs")

    # Recreate the 0004 shape directly for a lossless downgrade of compatible rows.
    op.rename_table("_dc041a_handoffs_backup", "handoffs")
    op.create_index(
        "ix_handoffs_project_work_status",
        "handoffs",
        ["project_id", "work_item_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_handoffs_active",
        "handoffs",
        ["project_id", "work_item_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('OPEN','DECIDED')"),
    )
    _create_decisions(extended=False)
    op.execute(sa.text(
        f"INSERT INTO decisions ({_DECISION_COLUMNS}) "
        f"SELECT {_DECISION_COLUMNS} FROM _dc041a_decisions_backup"
    ))
    op.drop_table("_dc041a_decisions_backup")
