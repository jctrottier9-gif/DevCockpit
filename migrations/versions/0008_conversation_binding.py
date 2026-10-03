"""Durable AgentSession to ChatGPT conversation binding.

Revision ID: 0008_conversation_binding
Revises: 0007_resource_locks
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_conversation_binding"
down_revision: Union[str, None] = "0007_resource_locks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_bindings",
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column("agent_session", sa.String(length=450), nullable=False),
        sa.Column("conversation_id", sa.String(length=500), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('BOUND','INVALIDATED')",
            name="ck_conversation_bindings_state",
        ),
        sa.CheckConstraint(
            "version >= 1 AND length(trim(agent_session)) > 0 "
            "AND length(trim(conversation_id)) > 0 AND length(trim(canonical_url)) > 0",
            name="ck_conversation_bindings_identity",
        ),
        sa.CheckConstraint(
            "(state = 'BOUND' AND invalidated_at IS NULL AND invalidation_reason IS NULL) "
            "OR (state = 'INVALIDATED' AND invalidated_at IS NOT NULL "
            "AND invalidation_reason IS NOT NULL AND length(trim(invalidation_reason)) > 0)",
            name="ck_conversation_bindings_invalidation",
        ),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint(
            "agent_session",
            name="uq_conversation_bindings_agent_session",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_conversation_bindings_conversation_id",
        ),
        sa.UniqueConstraint(
            "canonical_url",
            name="uq_conversation_bindings_canonical_url",
        ),
    )
    op.create_index(
        "ix_conversation_bindings_state",
        "conversation_bindings",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_bindings_state", table_name="conversation_bindings")
    op.drop_table("conversation_bindings")
