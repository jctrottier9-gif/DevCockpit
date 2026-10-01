"""Create PromptDispatch persistence.

Revision ID: 0001_prompt_dispatch
Revises:
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_prompt_dispatch"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_dispatches",
        sa.Column("dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("agent_session", sa.String(length=450), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('PO', 'ARCH', 'DEV')", name="ck_prompt_dispatches_role"),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'CANCELLED')",
            name="ck_prompt_dispatches_status",
        ),
        sa.PrimaryKeyConstraint("dispatch_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_prompt_dispatches_idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("prompt_dispatches")
