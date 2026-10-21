"""reward purchases fulfillment and inventory history

Revision ID: 0007_rewards_purchase_and_inventory
Revises: 0006_behavior_history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_rewards_purchase_and_inventory"
down_revision: Union[str, Sequence[str], None] = "0006_behavior_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reward_purchases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reward_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reward_code_snapshot", sa.Text(), nullable=False),
        sa.Column("reward_name_snapshot", sa.Text(), nullable=False),
        sa.Column("reward_type_snapshot", sa.Text(), nullable=False),
        sa.Column("price_snapshot", sa.Integer(), nullable=False),
        sa.Column("fulfillment_status", sa.Text(), nullable=False, server_default=sa.text("'unused'")),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("purchased_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "classroom_id"],
            ["enrollments.id", "enrollments.classroom_id"],
            name="fk_reward_purchases__enrollment_classroom__enrollments",
        ),
        sa.ForeignKeyConstraint(
            ["reward_id", "classroom_id"],
            ["rewards.id", "rewards.classroom_id"],
            name="fk_reward_purchases__reward_classroom__rewards",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_transaction_id", "classroom_id", "enrollment_id"],
            ["financial_transactions.id", "financial_transactions.classroom_id", "financial_transactions.enrollment_id"],
            name="fk_reward_purchases__transaction_account__fin_tx",
        ),
        sa.ForeignKeyConstraint(["purchased_by_user_id"], ["users.id"], name="fk_reward_purchases__purchased_by_user_id__users"),
        sa.UniqueConstraint("purchase_transaction_id", name="uq_reward_purchases__purchase_transaction_id"),
        sa.UniqueConstraint("id", "classroom_id", name="uq_reward_purchases__id_classroom"),
        sa.CheckConstraint("reward_type_snapshot IN ('physical','privilege','other')", name="ck_reward_purchases__reward_type_snapshot"),
        sa.CheckConstraint("price_snapshot > 0", name="ck_reward_purchases__price_positive"),
        sa.CheckConstraint("fulfillment_status IN ('unused','redeemed','cancelled')", name="ck_reward_purchases__fulfillment_status"),
    )
    op.create_index("ix_reward_purchases__enrollment_purchased", "reward_purchases", ["enrollment_id", sa.text("purchased_at DESC")])
    op.create_index("ix_reward_purchases__classroom_purchased", "reward_purchases", ["classroom_id", sa.text("purchased_at DESC")])

    op.create_table(
        "reward_fulfillment_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reward_purchase_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["reward_purchase_id"], ["reward_purchases.id"], name="fk_reward_fulfillment_events__purchase_id__reward_purchases"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_reward_fulfillment_events__actor_user_id__users"),
        sa.CheckConstraint("from_status IS NULL OR from_status IN ('unused','redeemed','cancelled')", name="ck_reward_fulfillment_events__from_status"),
        sa.CheckConstraint("to_status IN ('unused','redeemed','cancelled')", name="ck_reward_fulfillment_events__to_status"),
    )
    op.create_index("ix_reward_fulfillment_events__purchase_occurred", "reward_fulfillment_events", ["reward_purchase_id", "occurred_at"])

    op.create_table(
        "reward_inventory_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("classroom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reward_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reward_purchase_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("financial_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("quantity_before", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["reward_id", "classroom_id"],
            ["rewards.id", "rewards.classroom_id"],
            name="fk_reward_inventory_events__reward_classroom__rewards",
        ),
        sa.ForeignKeyConstraint(
            ["reward_purchase_id", "classroom_id"],
            ["reward_purchases.id", "reward_purchases.classroom_id"],
            name="fk_reward_inventory__purchase_classroom__reward_purchases",
        ),
        sa.ForeignKeyConstraint(
            ["financial_transaction_id", "classroom_id"],
            ["financial_transactions.id", "financial_transactions.classroom_id"],
            name="fk_reward_inventory__transaction_classroom__fin_tx",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_reward_inventory_events__actor_user_id__users"),
        sa.CheckConstraint(
            "event_type IN ('initial_stock','purchase_decrement','refund_restore','reversal_decrement','manual_adjustment')",
            name="ck_reward_inventory_events__event_type",
        ),
        sa.CheckConstraint("delta <> 0", name="ck_reward_inventory_events__delta_nonzero"),
        sa.CheckConstraint("quantity_before >= 0", name="ck_reward_inventory_events__quantity_before_nonnegative"),
        sa.CheckConstraint("quantity_after >= 0", name="ck_reward_inventory_events__quantity_after_nonnegative"),
        sa.CheckConstraint("quantity_after = quantity_before + delta", name="ck_reward_inventory_events__quantity_arithmetic"),
    )
    op.create_index(
        "ux_reward_inventory_events__financial_transaction_id",
        "reward_inventory_events",
        ["financial_transaction_id"],
        unique=True,
        postgresql_where=sa.text("financial_transaction_id IS NOT NULL"),
    )
    op.create_index("ix_reward_inventory_events__reward_occurred", "reward_inventory_events", ["reward_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_reward_inventory_events__reward_occurred", table_name="reward_inventory_events")
    op.drop_index("ux_reward_inventory_events__financial_transaction_id", table_name="reward_inventory_events")
    op.drop_table("reward_inventory_events")
    op.drop_index("ix_reward_fulfillment_events__purchase_occurred", table_name="reward_fulfillment_events")
    op.drop_table("reward_fulfillment_events")
    op.drop_index("ix_reward_purchases__classroom_purchased", table_name="reward_purchases")
    op.drop_index("ix_reward_purchases__enrollment_purchased", table_name="reward_purchases")
    op.drop_table("reward_purchases")
