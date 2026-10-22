"""Preserve manual financial transaction categories.

Revision ID: 0011_manual_financial_category
Revises: 0010_financial_hardening
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_manual_financial_category"
down_revision: Union[str, Sequence[str], None] = "0010_financial_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_transactions", sa.Column("manual_category", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_financial_transactions__manual_category",
        "financial_transactions",
        """(transaction_type = 'manual' AND manual_category IN
        ('general', 'behavior_positive', 'behavior_negative'))
        OR (transaction_type <> 'manual' AND manual_category IS NULL)""",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_financial_transactions__manual_category",
        "financial_transactions",
        type_="check",
    )
    op.drop_column("financial_transactions", "manual_category")
