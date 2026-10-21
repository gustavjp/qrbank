"""administrative audit log

Revision ID: 0009_administrative_audit
Revises: 0008_automation_and_scheduled_payments
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_administrative_audit"
down_revision: Union[str, Sequence[str], None] = "0008_automation_and_scheduled_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_admin_audit_events__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_admin_audit_events__actor_user_id__users"),
    )
    op.create_index("ix_admin_audit_events__classroom_occurred", "admin_audit_events", ["classroom_id", sa.text("occurred_at DESC")])
    op.create_index("ix_admin_audit_events__target_occurred", "admin_audit_events", ["target_type", "target_id", sa.text("occurred_at DESC")])
    op.create_index("ix_admin_audit_events__actor_occurred", "admin_audit_events", ["actor_user_id", sa.text("occurred_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_events__actor_occurred", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events__target_occurred", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events__classroom_occurred", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
