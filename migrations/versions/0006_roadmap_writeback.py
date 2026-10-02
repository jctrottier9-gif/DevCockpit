"""Roadmap writeback applications, confirmation metadata and poller fencing.

Revision ID: 0006_roadmap_writeback
Revises: 0005_po_roadmap_proposals
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_roadmap_writeback"
down_revision: Union[str, None] = "0005_po_roadmap_proposals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROPOSAL_0005_COLUMNS = (
    "proposal_id, project_id, repository_full_name, roadmap_issue_number, "
    "source_decision_id, status, version, current_revision, creation_command_id, "
    "created_by, created_at, cancelled_by, cancelled_at, cancellation_command_id"
)
_REVISION_COLUMNS = (
    "proposal_id, revision, base_body, base_body_hash, base_updated_at, proposed_body, "
    "proposed_body_hash, operations_json, generator_version, validation_version, "
    "created_by, created_at, revision_command_id"
)


def _create_proposals(*, extended: bool) -> None:
    status_check = (
        "status IN ('DRAFT','CONFIRMED','APPLIED','CANCELLED')"
        if extended
        else "status IN ('DRAFT','CANCELLED')"
    )
    columns = [
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
    ]
    if extended:
        columns.extend([
            sa.Column("confirmed_revision", sa.Integer(), nullable=True),
            sa.Column("confirmed_preview_digest", sa.String(length=64), nullable=True),
            sa.Column("confirmation_command_id", sa.String(length=36), nullable=True),
            sa.Column("confirmed_by", sa.Text(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("writeback_authorization_decision_id", sa.String(length=36), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        ])

    constraints = [
        sa.CheckConstraint(status_check, name="ck_roadmap_proposal_status"),
        sa.CheckConstraint(
            "version >= 1 AND current_revision >= 1",
            name="ck_roadmap_proposal_version",
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
    ]
    if extended:
        constraints.extend([
            sa.CheckConstraint(
                "(status IN ('CONFIRMED','APPLIED') "
                "AND confirmed_revision IS NOT NULL AND confirmed_revision >= 1 "
                "AND confirmed_preview_digest IS NOT NULL "
                "AND length(confirmed_preview_digest) = 64 "
                "AND confirmation_command_id IS NOT NULL "
                "AND confirmed_by IS NOT NULL AND length(trim(confirmed_by)) > 0 "
                "AND confirmed_at IS NOT NULL "
                "AND writeback_authorization_decision_id IS NOT NULL) "
                "OR (status NOT IN ('CONFIRMED','APPLIED') "
                "AND confirmed_revision IS NULL AND confirmed_preview_digest IS NULL "
                "AND confirmation_command_id IS NULL AND confirmed_by IS NULL "
                "AND confirmed_at IS NULL AND writeback_authorization_decision_id IS NULL)",
                name="ck_roadmap_proposal_confirmation",
            ),
            sa.CheckConstraint(
                "(status = 'APPLIED' AND applied_at IS NOT NULL) "
                "OR (status != 'APPLIED' AND applied_at IS NULL)",
                name="ck_roadmap_proposal_applied",
            ),
            sa.ForeignKeyConstraint(
                ["writeback_authorization_decision_id"],
                ["decisions.decision_id"],
                ondelete="RESTRICT",
            ),
            sa.UniqueConstraint(
                "confirmation_command_id",
                name="uq_roadmap_proposal_confirmation_command",
            ),
        ])

    op.create_table("roadmap_change_proposals", *columns, *constraints)


def _create_revisions() -> None:
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
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_roadmap_proposal_revision_number",
        ),
        sa.CheckConstraint(
            "length(base_body_hash) = 64 AND length(proposed_body_hash) = 64",
            name="ck_roadmap_proposal_revision_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["roadmap_change_proposals.proposal_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("proposal_id", "revision"),
        sa.UniqueConstraint(
            "revision_command_id",
            name="uq_roadmap_proposal_revision_command",
        ),
    )


def _backup_0005_proposals_and_revisions() -> None:
    op.create_table(
        "_dc041b_revisions_backup",
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
    )
    op.execute(sa.text(
        f"INSERT INTO _dc041b_revisions_backup ({_REVISION_COLUMNS}) "
        f"SELECT {_REVISION_COLUMNS} FROM roadmap_change_proposal_revisions"
    ))
    op.create_table(
        "_dc041b_proposals_backup",
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
    )
    op.execute(sa.text(
        f"INSERT INTO _dc041b_proposals_backup ({_PROPOSAL_0005_COLUMNS}) "
        f"SELECT {_PROPOSAL_0005_COLUMNS} FROM roadmap_change_proposals"
    ))


def upgrade() -> None:
    _backup_0005_proposals_and_revisions()
    op.drop_table("roadmap_change_proposal_revisions")
    op.drop_table("roadmap_change_proposals")

    _create_proposals(extended=True)
    _create_revisions()

    op.execute(sa.text(
        f"INSERT INTO roadmap_change_proposals ({_PROPOSAL_0005_COLUMNS}) "
        f"SELECT {_PROPOSAL_0005_COLUMNS} FROM _dc041b_proposals_backup"
    ))
    op.execute(sa.text(
        f"INSERT INTO roadmap_change_proposal_revisions ({_REVISION_COLUMNS}) "
        f"SELECT {_REVISION_COLUMNS} FROM _dc041b_revisions_backup"
    ))
    op.drop_table("_dc041b_revisions_backup")
    op.drop_table("_dc041b_proposals_backup")

    op.create_table(
        "roadmap_change_applications",
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=200), nullable=False),
        sa.Column("repository_full_name", sa.String(length=300), nullable=False),
        sa.Column("roadmap_issue_number", sa.Integer(), nullable=False),
        sa.Column("base_body", sa.Text(), nullable=False),
        sa.Column("base_body_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_body", sa.Text(), nullable=False),
        sa.Column("expected_body_hash", sa.String(length=64), nullable=False),
        sa.Column("application_command_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_remote_body_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('PREPARED','APPLYING','APPLIED','NOT_APPLIED','CONFLICT','RECONCILIATION_REQUIRED')",
            name="ck_roadmap_application_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND roadmap_issue_number >= 1 AND version >= 1",
            name="ck_roadmap_application_numbers",
        ),
        sa.CheckConstraint(
            "length(base_body_hash) = 64 AND length(expected_body_hash) = 64",
            name="ck_roadmap_application_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["roadmap_change_proposals.proposal_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("application_id"),
        sa.UniqueConstraint("application_command_id"),
    )
    op.create_index(
        "ix_roadmap_applications_target_status",
        "roadmap_change_applications",
        ["repository_full_name", "roadmap_issue_number", "status"],
        unique=False,
    )

    op.create_table(
        "roadmap_change_application_attempts",
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.String(length=36), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("patch_may_have_been_emitted", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_roadmap_application_attempt_number",
        ),
        sa.CheckConstraint(
            "outcome IN ('PREPARED','APPLYING','NOT_EMITTED','WRITE_RETURNED','APPLIED','CONFLICT','RECONCILIATION_REQUIRED')",
            name="ck_roadmap_application_attempt_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["roadmap_change_applications.application_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("command_id"),
        sa.UniqueConstraint(
            "application_id",
            "attempt_number",
            name="uq_roadmap_application_attempt_number",
        ),
    )

    op.create_table(
        "roadmap_target_fences",
        sa.Column("repository_full_name", sa.String(length=300), nullable=False),
        sa.Column("roadmap_issue_number", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("active_application_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "roadmap_issue_number >= 1 AND generation >= 0",
            name="ck_roadmap_target_fence_numbers",
        ),
        sa.ForeignKeyConstraint(
            ["active_application_id"],
            ["roadmap_change_applications.application_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("repository_full_name", "roadmap_issue_number"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    unsupported = connection.execute(sa.text(
        "SELECT COUNT(*) FROM roadmap_change_proposals "
        "WHERE status NOT IN ('DRAFT','CANCELLED')"
    )).scalar_one()
    if unsupported:
        raise RuntimeError(
            "Cannot downgrade DC-041B while CONFIRMED/APPLIED proposals exist"
        )

    op.drop_table("roadmap_target_fences")
    op.drop_table("roadmap_change_application_attempts")
    op.drop_index(
        "ix_roadmap_applications_target_status",
        table_name="roadmap_change_applications",
    )
    op.drop_table("roadmap_change_applications")

    _backup_0005_proposals_and_revisions()
    op.drop_table("roadmap_change_proposal_revisions")
    op.drop_table("roadmap_change_proposals")

    _create_proposals(extended=False)
    _create_revisions()
    op.execute(sa.text(
        f"INSERT INTO roadmap_change_proposals ({_PROPOSAL_0005_COLUMNS}) "
        f"SELECT {_PROPOSAL_0005_COLUMNS} FROM _dc041b_proposals_backup"
    ))
    op.execute(sa.text(
        f"INSERT INTO roadmap_change_proposal_revisions ({_REVISION_COLUMNS}) "
        f"SELECT {_REVISION_COLUMNS} FROM _dc041b_revisions_backup"
    ))
    op.drop_table("_dc041b_revisions_backup")
    op.drop_table("_dc041b_proposals_backup")
