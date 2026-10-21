"""financial reversal database hardening

Revision ID: 0010_financial_hardening
Revises: 0009_administrative_audit
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_financial_hardening"
down_revision: Union[str, Sequence[str], None] = "0009_administrative_audit"
branch_labels = None
depends_on = None


FUNCTION_SQL = r"""
CREATE FUNCTION qrbank_validate_financial_reversal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_amount integer;
BEGIN
    IF NEW.reverses_transaction_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT amount
      INTO target_amount
      FROM financial_transactions
     WHERE id = NEW.reverses_transaction_id
       AND classroom_id = NEW.classroom_id
       AND enrollment_id = NEW.enrollment_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'reversal target does not exist for this classroom/enrollment'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.amount <> -target_amount THEN
        RAISE EXCEPTION 'reversal amount % must equal exact negative of target amount %', NEW.amount, target_amount
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;
"""

TRIGGER_SQL = r"""
CREATE CONSTRAINT TRIGGER ck_financial_transactions__reversal_exact_amount
AFTER INSERT ON financial_transactions
DEFERRABLE INITIALLY IMMEDIATE
FOR EACH ROW
WHEN (NEW.reverses_transaction_id IS NOT NULL)
EXECUTE FUNCTION qrbank_validate_financial_reversal();
"""


def upgrade() -> None:
    op.execute(FUNCTION_SQL)
    op.execute(TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER ck_financial_transactions__reversal_exact_amount ON financial_transactions")
    op.execute("DROP FUNCTION qrbank_validate_financial_reversal()")
