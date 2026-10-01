"""handoffs_decisions

Revision ID: 0004_handoffs_decisions
Revises: 0003_imported_chatgpt_response
Create Date: 2026-10-01 14:19:19.319686
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004_handoffs_decisions'
down_revision: Union[str, None] = '0003_imported_chatgpt_response'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('handoffs',
    sa.Column('handoff_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=200), nullable=False),
    sa.Column('work_item_id', sa.String(length=200), nullable=False),
    sa.Column('source_dispatch_id', sa.String(length=36), nullable=False),
    sa.Column('source_response_id', sa.String(length=36), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('context', sa.Text(), nullable=False),
    sa.Column('context_snapshot', sa.Text(), nullable=False),
    sa.Column('request_dispatch_id', sa.String(length=36), nullable=False),
    sa.Column('resume_dispatch_id', sa.String(length=36), nullable=True),
    sa.Column('creation_command_id', sa.String(length=36), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('target_role', sa.String(length=16), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('covered_github_evidence', sa.Text(), nullable=True),
    sa.Column('resume_held_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_by', sa.Text(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('cancellation_command_id', sa.String(length=36), nullable=True),
    sa.CheckConstraint("(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancel_reason IS NOT NULL AND length(trim(cancelled_by)) > 0 AND length(trim(cancel_reason)) > 0 AND cancellation_command_id IS NOT NULL) OR (status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL AND cancel_reason IS NULL AND cancellation_command_id IS NULL)", name='ck_handoff_cancel'),
    sa.CheckConstraint("(status = 'RESUME_PREPARED' AND resume_dispatch_id IS NOT NULL) OR (status != 'RESUME_PREPARED' AND resume_dispatch_id IS NULL)", name='ck_handoff_resume'),
    sa.CheckConstraint("status IN ('OPEN','DECIDED','RESUME_PREPARED','CANCELLED')", name='ck_handoff_status'),
    sa.CheckConstraint("target_role = 'ARCH' AND purpose = 'TECHNICAL_GUIDANCE'", name='ck_handoff_role'),
    sa.CheckConstraint('length(trim(question)) > 0 AND length(trim(context)) > 0 AND length(trim(context_snapshot)) > 0 AND length(trim(created_by)) > 0', name='ck_handoff_text'),
    sa.CheckConstraint('version >= 1', name='ck_handoff_version'),
    sa.ForeignKeyConstraint(['request_dispatch_id'], ['prompt_dispatches.dispatch_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['resume_dispatch_id'], ['prompt_dispatches.dispatch_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_dispatch_id'], ['prompt_dispatches.dispatch_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_response_id'], ['imported_chatgpt_responses.response_id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('handoff_id'),
    sa.UniqueConstraint('cancellation_command_id'),
    sa.UniqueConstraint('creation_command_id'),
    sa.UniqueConstraint('request_dispatch_id'),
    sa.UniqueConstraint('resume_dispatch_id')
    )
    op.create_index('ix_handoffs_project_work_status', 'handoffs', ['project_id', 'work_item_id', 'status'], unique=False)
    op.create_index('uq_handoffs_active', 'handoffs', ['project_id', 'work_item_id'], unique=True, sqlite_where=sa.text("status IN ('OPEN','DECIDED')"))
    op.create_table('decisions',
    sa.Column('decision_id', sa.String(length=36), nullable=False),
    sa.Column('source_handoff_id', sa.String(length=36), nullable=False),
    sa.Column('source_response_id', sa.String(length=36), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('effect', sa.String(length=32), nullable=False),
    sa.Column('accepted_by', sa.Text(), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('acceptance_command_id', sa.String(length=36), nullable=False),
    sa.Column('decision_type', sa.String(length=32), nullable=False),
    sa.CheckConstraint("decision_type = 'ARCHITECTURE_GUIDANCE'", name='ck_decision_type'),
    sa.CheckConstraint("effect IN ('CONTINUE_IN_SCOPE','HOLD_FOR_AUTHORIZATION')", name='ck_decision_effect'),
    sa.CheckConstraint('length(trim(summary)) > 0 AND length(trim(accepted_by)) > 0', name='ck_decision_text'),
    sa.ForeignKeyConstraint(['source_handoff_id'], ['handoffs.handoff_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_response_id'], ['imported_chatgpt_responses.response_id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('decision_id'),
    sa.UniqueConstraint('acceptance_command_id'),
    sa.UniqueConstraint('source_handoff_id')
    )


def downgrade() -> None:
    op.drop_table('decisions')
    op.drop_index('uq_handoffs_active', table_name='handoffs', sqlite_where=sa.text("status IN ('OPEN','DECIDED')"))
    op.drop_index('ix_handoffs_project_work_status', table_name='handoffs')
    op.drop_table('handoffs')
