"""Store immutable accepted delivery contexts (DC-075A).

Revision ID: 0011_delivery_contexts
Revises: 0010_pr_finalization_attempts
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011_delivery_contexts"
down_revision: Union[str, None] = "0010_pr_finalization_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_contexts",
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("repository_full_name", sa.String(length=250), nullable=False),
        sa.Column("delivery_issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_body_sha256", sa.String(length=64), nullable=False),
        sa.Column("contract_json", sa.Text(), nullable=False),
        sa.Column("contract_sha256", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "work_item_id"),
        sa.UniqueConstraint("repository_id", "work_item_id",
                            name="uq_delivery_context_work_item"),
    )


def downgrade() -> None:
    op.drop_table("delivery_contexts")
