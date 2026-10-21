"""classroom actions behavior reasons jobs and rewards

Revision ID: 0004_classroom_definitions
Revises: 0003_students_and_enrollments
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_classroom_definitions"
down_revision: Union[str, Sequence[str], None] = "0003_students_and_enrollments"
branch_labels = None
depends_on = None

CODE_RE = "^[A-Z0-9_-]{1,32}$"


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_actions__classroom_id__classrooms"),
        sa.UniqueConstraint("classroom_id", "action_code", name="uq_actions__classroom_action_code"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_actions__id_classroom"),
        sa.CheckConstraint(f"action_code ~ '{CODE_RE}'", name="ck_actions__action_code_format"),
        sa.CheckConstraint("action_kind IN ('general','behavior_positive','behavior_negative')", name="ck_actions__action_kind"),
    )

    op.create_table(
        "quick_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quick_action_code", sa.Text(), nullable=False),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_quick_actions__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(
            ["action_id", "classroom_id"],
            ["actions.id", "actions.classroom_id"],
            name="fk_quick_actions__action_classroom__actions",
        ),
        sa.UniqueConstraint("classroom_id", "quick_action_code", name="uq_quick_actions__classroom_code"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_quick_actions__id_classroom"),
        sa.CheckConstraint(f"quick_action_code ~ '{CODE_RE}'", name="ck_quick_actions__code_format"),
        sa.CheckConstraint("display_order >= 0", name="ck_quick_actions__display_order"),
    )

    op.create_table(
        "behavior_reasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("behavior_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_behavior_reasons__classroom_id__classrooms"),
        sa.UniqueConstraint("classroom_id", "reason_code", name="uq_behavior_reasons__classroom_code"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_behavior_reasons__id_classroom"),
        sa.UniqueConstraint("id", "classroom_id", "behavior_type", name="uq_behavior_reasons__id_classroom_type"),
        sa.CheckConstraint(f"reason_code ~ '{CODE_RE}'", name="ck_behavior_reasons__code_format"),
        sa.CheckConstraint("behavior_type IN ('positive','negative')", name="ck_behavior_reasons__behavior_type"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("weekly_wage", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_jobs__classroom_id__classrooms"),
        sa.UniqueConstraint("classroom_id", "job_code", name="uq_jobs__classroom_job_code"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_jobs__id_classroom"),
        sa.CheckConstraint(f"job_code ~ '{CODE_RE}'", name="ck_jobs__job_code_format"),
        sa.CheckConstraint("weekly_wage >= 0", name="ck_jobs__weekly_wage_nonnegative"),
    )

    op.create_table(
        "job_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("ended_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "classroom_id"],
            ["enrollments.id", "enrollments.classroom_id"],
            name="fk_job_assignments__enrollment_classroom__enrollments",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "classroom_id"],
            ["jobs.id", "jobs.classroom_id"],
            name="fk_job_assignments__job_classroom__jobs",
        ),
        sa.ForeignKeyConstraint(["ended_by_user_id"], ["users.id"], name="fk_job_assignments__ended_by_user_id__users"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_job_assignments__id_classroom"),
        sa.CheckConstraint("ended_on IS NULL OR ended_on >= started_on", name="ck_job_assignments__end_after_start"),
    )
    op.create_index(
        "ux_job_assignments__active_enrollment_job",
        "job_assignments",
        ["enrollment_id", "job_id"],
        unique=True,
        postgresql_where=sa.text("ended_on IS NULL"),
    )
    op.create_index(
        "ix_job_assignments__active_classroom_enrollment",
        "job_assignments",
        ["classroom_id", "enrollment_id"],
        postgresql_where=sa.text("ended_on IS NULL"),
    )
    op.create_index(
        "ix_job_assignments__active_job",
        "job_assignments",
        ["job_id"],
        postgresql_where=sa.text("ended_on IS NULL"),
    )

    op.create_table(
        "rewards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reward_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reward_type", sa.Text(), nullable=False),
        sa.Column("price_points", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("inventory_tracking", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quantity_available", sa.Integer(), nullable=True),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("image_storage_key", sa.Text(), nullable=True),
        sa.Column("image_content_type", sa.Text(), nullable=True),
        sa.Column("image_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_rewards__classroom_id__classrooms"),
        sa.UniqueConstraint("classroom_id", "reward_code", name="uq_rewards__classroom_reward_code"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_rewards__id_classroom"),
        sa.CheckConstraint(f"reward_code ~ '{CODE_RE}'", name="ck_rewards__reward_code_format"),
        sa.CheckConstraint("reward_type IN ('physical','privilege','other')", name="ck_rewards__reward_type"),
        sa.CheckConstraint("price_points > 0", name="ck_rewards__price_positive"),
        sa.CheckConstraint(
            "(inventory_tracking AND quantity_available IS NOT NULL AND quantity_available >= 0) OR "
            "(NOT inventory_tracking AND quantity_available IS NULL)",
            name="ck_rewards__inventory_consistency",
        ),
        sa.CheckConstraint(
            "available_until IS NULL OR available_from IS NULL OR available_until > available_from",
            name="ck_rewards__availability_order",
        ),
        sa.CheckConstraint("image_size_bytes IS NULL OR image_size_bytes >= 0", name="ck_rewards__image_size_nonnegative"),
    )
    op.create_index("ix_rewards__classroom_active_code", "rewards", ["classroom_id", "active", "reward_code"])


def downgrade() -> None:
    op.drop_index("ix_rewards__classroom_active_code", table_name="rewards")
    op.drop_table("rewards")
    op.drop_index("ix_job_assignments__active_job", table_name="job_assignments")
    op.drop_index("ix_job_assignments__active_classroom_enrollment", table_name="job_assignments")
    op.drop_index("ux_job_assignments__active_enrollment_job", table_name="job_assignments")
    op.drop_table("job_assignments")
    op.drop_table("jobs")
    op.drop_table("behavior_reasons")
    op.drop_table("quick_actions")
    op.drop_table("actions")
