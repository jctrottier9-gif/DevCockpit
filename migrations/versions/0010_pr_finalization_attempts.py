"""Persist deterministic pull-request finalization attempts.

Revision ID: 0010_pr_finalization_attempts
Revises: 0009_chatgpt_prompt_send
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_pr_finalization_attempts"
down_revision: Union[str, None] = "0009_chatgpt_prompt_send"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pull_request_finalization_attempts",
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("expected_head_sha", sa.String(length=64), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("requires_dev", sa.Boolean(), nullable=False),
        sa.Column("resulting_head_sha", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('SYNC_BRANCH','MERGE_PR')",
            name="ck_pr_finalization_operation",
        ),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS','SUCCEEDED','BLOCKED','STALE')",
            name="ck_pr_finalization_status",
        ),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_pr_finalization_work_item",
        "pull_request_finalization_attempts",
        ["project_id", "work_item_id", "pr_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pr_finalization_work_item",
        table_name="pull_request_finalization_attempts",
    )
    op.drop_table("pull_request_finalization_attempts")
