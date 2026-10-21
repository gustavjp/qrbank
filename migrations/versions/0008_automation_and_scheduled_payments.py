"""automation run and scheduled payment idempotency

Revision ID: 0008_automation_and_scheduled_payments
Revises: 0007_rewards_purchase_and_inventory
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008_automation_and_scheduled_payments"
down_revision: Union[str, Sequence[str], None] = "0007_rewards_purchase_and_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_automation_runs__classroom_id__classrooms"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_automation_runs__id_classroom"),
        sa.CheckConstraint("run_type IN ('allowance','job_wages')", name="ck_automation_runs__run_type"),
        sa.CheckConstraint("status IN ('running','completed','partial','failed')", name="ck_automation_runs__status"),
        sa.CheckConstraint("attempted_count >= 0", name="ck_automation_runs__attempted_count"),
        sa.CheckConstraint("completed_count >= 0", name="ck_automation_runs__completed_count"),
        sa.CheckConstraint("rejected_count >= 0", name="ck_automation_runs__rejected_count"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_automation_runs__finish_after_start"),
    )
    op.create_index(
        "ix_automation_runs__classroom_type_period_started",
        "automation_runs",
        ["classroom_id", "run_type", "period_key", sa.text("started_at DESC")],
    )

    op.create_table(
        "scheduled_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_type", sa.Text(), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column("amount_snapshot", sa.Integer(), nullable=False),
        sa.Column("automation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("financial_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "classroom_id"],
            ["enrollments.id", "enrollments.classroom_id"],
            name="fk_scheduled_payments__enrollment_classroom__enrollments",
        ),
        sa.ForeignKeyConstraint(
            ["job_assignment_id", "classroom_id"],
            ["job_assignments.id", "job_assignments.classroom_id"],
            name="fk_scheduled_payments__job_assignment__job_assignments",
        ),
        sa.ForeignKeyConstraint(
            ["automation_run_id", "classroom_id"],
            ["automation_runs.id", "automation_runs.classroom_id"],
            name="fk_scheduled_payments__automation_run__automation_runs",
        ),
        sa.ForeignKeyConstraint(
            ["financial_transaction_id", "classroom_id", "enrollment_id"],
            ["financial_transactions.id", "financial_transactions.classroom_id", "financial_transactions.enrollment_id"],
            name="fk_scheduled_payments__transaction_account__fin_tx",
        ),
        sa.UniqueConstraint("financial_transaction_id", name="uq_scheduled_payments__financial_transaction_id"),
        sa.CheckConstraint("payment_type IN ('allowance','job_wage')", name="ck_scheduled_payments__payment_type"),
        sa.CheckConstraint("amount_snapshot >= 0", name="ck_scheduled_payments__amount_nonnegative"),
        sa.CheckConstraint(
            "(payment_type = 'allowance' AND job_assignment_id IS NULL) OR "
            "(payment_type = 'job_wage' AND job_assignment_id IS NOT NULL)",
            name="ck_scheduled_payments__job_assignment_shape",
        ),
    )
    op.create_index(
        "ux_scheduled_payments__allowance_period",
        "scheduled_payments",
        ["enrollment_id", "period_key"],
        unique=True,
        postgresql_where=sa.text("payment_type = 'allowance'"),
    )
    op.create_index(
        "ux_scheduled_payments__job_wage_period",
        "scheduled_payments",
        ["job_assignment_id", "period_key"],
        unique=True,
        postgresql_where=sa.text("payment_type = 'job_wage'"),
    )
    op.create_index("ix_scheduled_payments__classroom_type_period", "scheduled_payments", ["classroom_id", "payment_type", "period_key"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_payments__classroom_type_period", table_name="scheduled_payments")
    op.drop_index("ux_scheduled_payments__job_wage_period", table_name="scheduled_payments")
    op.drop_index("ux_scheduled_payments__allowance_period", table_name="scheduled_payments")
    op.drop_table("scheduled_payments")
    op.drop_index("ix_automation_runs__classroom_type_period_started", table_name="automation_runs")
    op.drop_table("automation_runs")
