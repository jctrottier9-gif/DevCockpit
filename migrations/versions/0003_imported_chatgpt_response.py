"""Create imported ChatGPT response persistence.

Revision ID: 0003_imported_chatgpt_response
Revises: 0002_prompt_delivery
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_imported_chatgpt_response"
down_revision = "0002_prompt_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "imported_chatgpt_responses",
        sa.Column("response_id", sa.String(length=36), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(text)) > 0",
            name="ck_imported_chatgpt_responses_text",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_imported_chatgpt_responses_delivery_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("response_id"),
    )
    op.create_index(
        "ix_imported_chatgpt_responses_delivery_id",
        "imported_chatgpt_responses",
        ["delivery_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_imported_chatgpt_responses_delivery_id",
        table_name="imported_chatgpt_responses",
    )
    op.drop_table("imported_chatgpt_responses")
