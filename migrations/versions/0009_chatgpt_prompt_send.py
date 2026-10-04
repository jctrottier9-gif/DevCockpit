"""Persist browser-to-ChatGPT send projection and replayable status evidence.

Revision ID: 0009_chatgpt_prompt_send
Revises: 0008_conversation_binding
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_chatgpt_prompt_send"
down_revision: Union[str, None] = "0008_conversation_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chatgpt_prompt_sends",
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("session", sa.String(length=450), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=200), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "state IN ('QUEUED','ROUTING','WAITING_READY','SEND_ARMED','SENT_CONFIRMED',"
            "'RETRYABLE_FAILURE','BLOCKED','AMBIGUOUS')",
            name="ck_chatgpt_prompt_sends_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_chatgpt_prompt_sends_attempt_count",
        ),
        sa.CheckConstraint(
            "(state = 'SENT_CONFIRMED' AND confirmed_at IS NOT NULL) OR "
            "(state <> 'SENT_CONFIRMED' AND confirmed_at IS NULL)",
            name="ck_chatgpt_prompt_sends_confirmed_at",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_chatgpt_prompt_sends_delivery_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "ix_chatgpt_prompt_sends_session_state",
        "chatgpt_prompt_sends",
        ["session", "state"],
        unique=False,
    )

    op.create_table(
        "chatgpt_send_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("session", sa.String(length=450), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(length=500), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_chatgpt_send_events_delivery_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_chatgpt_send_events_delivery",
        "chatgpt_send_events",
        ["delivery_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chatgpt_send_events_delivery", table_name="chatgpt_send_events")
    op.drop_table("chatgpt_send_events")
    op.drop_index("ix_chatgpt_prompt_sends_session_state", table_name="chatgpt_prompt_sends")
    op.drop_table("chatgpt_prompt_sends")
