"""Persistent ResourceLocks for deterministic parallel-execution conflicts.

Revision ID: 0007_resource_locks
Revises: 0006_roadmap_writeback
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_resource_locks"
down_revision: Union[str, None] = "0006_roadmap_writeback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_locks",
        sa.Column("lock_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("work_item_id", sa.String(length=200), nullable=False),
        sa.Column("agent_session", sa.String(length=450), nullable=False),
        sa.Column("surface", sa.String(length=400), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('SHARED','EXCLUSIVE')",
            name="ck_resource_lock_mode",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','RELEASED','STALE')",
            name="ck_resource_lock_state",
        ),
        sa.CheckConstraint(
            "version >= 1 AND length(trim(project_id)) > 0 "
            "AND length(trim(work_item_id)) > 0 AND length(trim(agent_session)) > 0 "
            "AND length(trim(surface)) > 0",
            name="ck_resource_lock_identity",
        ),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND released_at IS NULL AND release_reason IS NULL) "
            "OR (state IN ('RELEASED','STALE') AND released_at IS NOT NULL "
            "AND release_reason IS NOT NULL AND length(trim(release_reason)) > 0)",
            name="ck_resource_lock_release",
        ),
        sa.PrimaryKeyConstraint("lock_id"),
        sa.UniqueConstraint(
            "project_id",
            "work_item_id",
            "surface",
            name="uq_resource_lock_owner_surface",
        ),
    )
    op.create_index(
        "ix_resource_locks_project_state_surface",
        "resource_locks",
        ["project_id", "state", "surface"],
        unique=False,
    )
    op.create_index(
        "ix_resource_locks_owner_state",
        "resource_locks",
        ["project_id", "work_item_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resource_locks_owner_state",
        table_name="resource_locks",
    )
    op.drop_index(
        "ix_resource_locks_project_state_surface",
        table_name="resource_locks",
    )
    op.drop_table("resource_locks")
