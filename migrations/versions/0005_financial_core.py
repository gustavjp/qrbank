"""financial request and ledger core

Revision ID: 0005_financial_core
Revises: 0004_classroom_definitions
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_financial_core"
down_revision: Union[str, Sequence[str], None] = "0004_classroom_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE financial_transaction_display_number_seq START WITH 1 INCREMENT BY 1")

    op.create_table(
        "financial_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_kind", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rejection_code", sa.Text(), nullable=True),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_financial_requests__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_financial_requests__actor_user_id__users"),
        sa.UniqueConstraint("classroom_id", "idempotency_key", name="uq_financial_requests__classroom_idempotency_key"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_financial_requests__id_classroom"),
        sa.CheckConstraint("status IN ('completed','rejected')", name="ck_financial_requests__status"),
        sa.CheckConstraint(
            "(status = 'rejected' AND rejection_code IS NOT NULL) OR status = 'completed'",
            name="ck_financial_requests__rejection_code",
        ),
    )
    op.create_index("ix_financial_requests__classroom_created", "financial_requests", ["classroom_id", sa.text("created_at DESC")])

    op.create_table(
        "financial_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], name="fk_financial_batches__classroom_id__classrooms"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_financial_batches__actor_user_id__users"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_financial_batches__id_classroom"),
    )

    op.create_table(
        "financial_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "display_number",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('financial_transaction_display_number_seq')"),
        ),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("financial_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("financial_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_before", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reverses_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quick_action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reward_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("student_code_snapshot", sa.Text(), nullable=False),
        sa.Column("action_code_snapshot", sa.Text(), nullable=True),
        sa.Column("action_name_snapshot", sa.Text(), nullable=True),
        sa.Column("action_kind_snapshot", sa.Text(), nullable=True),
        sa.Column("quick_action_code_snapshot", sa.Text(), nullable=True),
        sa.Column("job_code_snapshot", sa.Text(), nullable=True),
        sa.Column("job_name_snapshot", sa.Text(), nullable=True),
        sa.Column("job_wage_snapshot", sa.Integer(), nullable=True),
        sa.Column("reward_code_snapshot", sa.Text(), nullable=True),
        sa.Column("reward_name_snapshot", sa.Text(), nullable=True),
        sa.Column("reward_price_snapshot", sa.Integer(), nullable=True),
        sa.Column("adjustment_target_balance", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "classroom_id"],
            ["enrollments.id", "enrollments.classroom_id"],
            name="fk_financial_transactions__enrollment_classroom__enrollments",
        ),
        sa.ForeignKeyConstraint(
            ["financial_request_id", "classroom_id"],
            ["financial_requests.id", "financial_requests.classroom_id"],
            name="fk_fin_tx__request_classroom__financial_requests",
        ),
        sa.ForeignKeyConstraint(
            ["financial_batch_id", "classroom_id"],
            ["financial_batches.id", "financial_batches.classroom_id"],
            name="fk_financial_transactions__batch_classroom__financial_batches",
        ),
        sa.ForeignKeyConstraint(
            ["action_id", "classroom_id"],
            ["actions.id", "actions.classroom_id"],
            name="fk_financial_transactions__action_classroom__actions",
        ),
        sa.ForeignKeyConstraint(
            ["quick_action_id", "classroom_id"],
            ["quick_actions.id", "quick_actions.classroom_id"],
            name="fk_fin_tx__quick_action_classroom__quick_actions",
        ),
        sa.ForeignKeyConstraint(
            ["job_assignment_id", "classroom_id"],
            ["job_assignments.id", "job_assignments.classroom_id"],
            name="fk_fin_tx__job_assignment_classroom__job_assignments",
        ),
        sa.ForeignKeyConstraint(
            ["reward_id", "classroom_id"],
            ["rewards.id", "rewards.classroom_id"],
            name="fk_financial_transactions__reward_classroom__rewards",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_financial_transactions__actor_user_id__users"),
        sa.UniqueConstraint("display_number", name="uq_financial_transactions__display_number"),
        sa.UniqueConstraint("financial_request_id", name="uq_financial_transactions__financial_request_id"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_financial_transactions__id_classroom"),
        sa.UniqueConstraint("id", "classroom_id", "enrollment_id", name="uq_financial_transactions__id_classroom_enrollment"),
        sa.CheckConstraint("amount <> 0", name="ck_financial_transactions__amount_nonzero"),
        sa.CheckConstraint("balance_after = balance_before + amount", name="ck_financial_transactions__balance_arithmetic"),
        sa.CheckConstraint("id <> reverses_transaction_id", name="ck_financial_transactions__not_self_reversal"),
        sa.CheckConstraint(
            "transaction_type IN ('opening_balance','action','manual','adjustment','allowance','job_wage','reward_purchase','reversal','bulk')",
            name="ck_financial_transactions__transaction_type",
        ),
        sa.CheckConstraint(
            "source IN ('scanner','phone','manual','automatic','reward_store','admin','bulk','system')",
            name="ck_financial_transactions__source",
        ),
        sa.CheckConstraint(
            "action_kind_snapshot IS NULL OR action_kind_snapshot IN ('general','behavior_positive','behavior_negative')",
            name="ck_financial_transactions__action_kind_snapshot",
        ),
    )

    op.create_foreign_key(
        "fk_fin_tx__reversal_account__financial_transactions",
        "financial_transactions",
        "financial_transactions",
        ["reverses_transaction_id", "classroom_id", "enrollment_id"],
        ["id", "classroom_id", "enrollment_id"],
    )
    op.create_index(
        "ux_financial_transactions__reverses_transaction_id",
        "financial_transactions",
        ["reverses_transaction_id"],
        unique=True,
        postgresql_where=sa.text("reverses_transaction_id IS NOT NULL"),
    )
    op.create_index(
        "ix_financial_transactions__enrollment_history",
        "financial_transactions",
        ["enrollment_id", sa.text("occurred_at DESC"), sa.text("display_number DESC")],
    )
    op.create_index(
        "ix_financial_transactions__classroom_history",
        "financial_transactions",
        ["classroom_id", sa.text("occurred_at DESC"), sa.text("display_number DESC")],
    )
    op.create_index(
        "ix_financial_transactions__action_history",
        "financial_transactions",
        ["action_id", sa.text("occurred_at DESC")],
        postgresql_where=sa.text("action_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_financial_transactions__action_history", table_name="financial_transactions")
    op.drop_index("ix_financial_transactions__classroom_history", table_name="financial_transactions")
    op.drop_index("ix_financial_transactions__enrollment_history", table_name="financial_transactions")
    op.drop_index("ux_financial_transactions__reverses_transaction_id", table_name="financial_transactions")
    op.drop_constraint("fk_fin_tx__reversal_account__financial_transactions", "financial_transactions", type_="foreignkey")
    op.drop_table("financial_transactions")
    op.drop_table("financial_batches")
    op.drop_index("ix_financial_requests__classroom_created", table_name="financial_requests")
    op.drop_table("financial_requests")
    op.execute("DROP SEQUENCE financial_transaction_display_number_seq")
