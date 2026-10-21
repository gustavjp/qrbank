"""behavior history

Revision ID: 0006_behavior_history
Revises: 0005_financial_core
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_behavior_history"
down_revision: Union[str, Sequence[str], None] = "0005_financial_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "behavior_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("behavior_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("financial_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_code_snapshot", sa.Text(), nullable=True),
        sa.Column("action_name_snapshot", sa.Text(), nullable=True),
        sa.Column("behavior_reason_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason_code_snapshot", sa.Text(), nullable=True),
        sa.Column("reason_name_snapshot", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reason_attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_attached_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "classroom_id"],
            ["enrollments.id", "enrollments.classroom_id"],
            name="fk_behavior_events__enrollment_classroom__enrollments",
        ),
        sa.ForeignKeyConstraint(
            ["financial_transaction_id", "classroom_id", "enrollment_id"],
            ["financial_transactions.id", "financial_transactions.classroom_id", "financial_transactions.enrollment_id"],
            name="fk_behavior_events__transaction_account__financial_transactions",
        ),
        sa.ForeignKeyConstraint(
            ["action_id", "classroom_id"],
            ["actions.id", "actions.classroom_id"],
            name="fk_behavior_events__action_classroom__actions",
        ),
        sa.ForeignKeyConstraint(
            ["behavior_reason_id", "classroom_id", "behavior_type"],
            ["behavior_reasons.id", "behavior_reasons.classroom_id", "behavior_reasons.behavior_type"],
            name="fk_behavior_events__reason_classroom_type__behavior_reasons",
        ),
        sa.ForeignKeyConstraint(["reason_attached_by_user_id"], ["users.id"], name="fk_behavior_events__reason_attached_by__users"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_behavior_events__created_by__users"),
        sa.CheckConstraint("behavior_type IN ('positive','negative')", name="ck_behavior_events__behavior_type"),
    )
    op.create_index(
        "ux_behavior_events__financial_transaction_id",
        "behavior_events",
        ["financial_transaction_id"],
        unique=True,
        postgresql_where=sa.text("financial_transaction_id IS NOT NULL"),
    )
    op.create_index("ix_behavior_events__enrollment_occurred", "behavior_events", ["enrollment_id", sa.text("occurred_at DESC")])
    op.create_index("ix_behavior_events__classroom_type_occurred", "behavior_events", ["classroom_id", "behavior_type", sa.text("occurred_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_behavior_events__classroom_type_occurred", table_name="behavior_events")
    op.drop_index("ix_behavior_events__enrollment_occurred", table_name="behavior_events")
    op.drop_index("ux_behavior_events__financial_transaction_id", table_name="behavior_events")
    op.drop_table("behavior_events")
