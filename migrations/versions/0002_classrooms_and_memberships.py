"""classrooms memberships and settings

Revision ID: 0002_classrooms_and_memberships
Revises: 0001_identity_and_auth
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_classrooms_and_memberships"
down_revision: Union[str, Sequence[str], None] = "0001_identity_and_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classrooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("school_year", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "classroom_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_classroom_memberships__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_classroom_memberships__user_id__users"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_classroom_memberships__created_by_user_id__users"),
        sa.CheckConstraint("role IN ('owner','teacher','assistant','school_admin')", name="ck_classroom_memberships__role"),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= joined_at", name="ck_classroom_memberships__end_after_join"),
    )
    op.create_index(
        "ux_classroom_memberships__active_user_classroom",
        "classroom_memberships",
        ["classroom_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ux_classroom_memberships__active_owner",
        "classroom_memberships",
        ["classroom_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND ended_at IS NULL"),
    )
    op.create_index(
        "ix_classroom_memberships__active_user_classroom",
        "classroom_memberships",
        ["user_id", "classroom_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "classroom_settings",
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'America/New_York'")),
        sa.Column("starting_balance", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("weekly_allowance_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("weekly_allowance_amount", sa.Integer(), nullable=False, server_default=sa.text("20")),
        sa.Column("weekly_allowance_day", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("weekly_allowance_time", sa.Time(), nullable=False, server_default=sa.text("'06:00:00'")),
        sa.Column("weekly_wages_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("weekly_wage_day", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("weekly_wage_time", sa.Time(), nullable=False, server_default=sa.text("'06:10:00'")),
        sa.Column("sound_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("duplicate_scan_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("allow_negative_balance", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("student_selection_timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("120")),
        sa.Column("confirm_normal_transactions", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirm_balance_reset", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_classroom_settings__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], name="fk_classroom_settings__updated_by_user_id__users"),
        sa.CheckConstraint("weekly_allowance_day BETWEEN 0 AND 6", name="ck_classroom_settings__allowance_day"),
        sa.CheckConstraint("weekly_wage_day BETWEEN 0 AND 6", name="ck_classroom_settings__wage_day"),
        sa.CheckConstraint("duplicate_scan_seconds >= 0", name="ck_classroom_settings__duplicate_scan_seconds"),
        sa.CheckConstraint("student_selection_timeout_seconds > 0", name="ck_classroom_settings__selection_timeout"),
    )


def downgrade() -> None:
    op.drop_table("classroom_settings")
    op.drop_index("ix_classroom_memberships__active_user_classroom", table_name="classroom_memberships")
    op.drop_index("ux_classroom_memberships__active_owner", table_name="classroom_memberships")
    op.drop_index("ux_classroom_memberships__active_user_classroom", table_name="classroom_memberships")
    op.drop_table("classroom_memberships")
    op.drop_table("classrooms")
