"""Create persistent prompt transport delivery state.

Revision ID: 0002_prompt_delivery
Revises: 0001_prompt_dispatch
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_prompt_delivery"
down_revision = "0001_prompt_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_deliveries",
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("dispatch_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACKNOWLEDGED')",
            name="ck_prompt_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_prompt_deliveries_attempt_count",
        ),
        sa.CheckConstraint(
            "(attempt_count = 0 AND last_attempt_at IS NULL) OR "
            "(attempt_count > 0 AND last_attempt_at IS NOT NULL)",
            name="ck_prompt_deliveries_attempt_timestamp",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND acknowledged_at IS NULL) OR "
            "(status = 'ACKNOWLEDGED' AND acknowledged_at IS NOT NULL AND attempt_count > 0)",
            name="ck_prompt_deliveries_ack_state",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["prompt_dispatches.dispatch_id"],
            name="fk_prompt_deliveries_dispatch_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("dispatch_id", name="uq_prompt_deliveries_dispatch_id"),
    )


def downgrade() -> None:
    op.drop_table("prompt_deliveries")
