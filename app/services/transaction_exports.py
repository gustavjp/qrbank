from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.financial import FinancialTransaction
from app.services.csv_security import safe_spreadsheet_text
from app.services.transaction_history import TransactionHistorySelection


# Column names remain machine-readable for pandas and other CSV consumers.
CSV_COLUMNS = (
    "transaction_id", "display_number", "classroom_id", "enrollment_id",
    "student_code_snapshot", "occurred_at_utc", "occurred_at_classroom_local",
    "classroom_timezone", "transaction_type", "source",
    "amount", "balance_before", "balance_after", "manual_category", "note",
    "action_id", "action_code_snapshot", "action_name_snapshot",
    "action_kind_snapshot", "quick_action_id", "quick_action_code_snapshot",
    "adjustment_target_balance", "reverses_transaction_id",
    "financial_request_id", "financial_batch_id", "actor_user_id",
)


def _transaction_cells(tx: FinancialTransaction, zone: ZoneInfo, timezone_name: str) -> tuple:
    return (
        str(tx.id),
        tx.display_number,
        str(tx.classroom_id),
        str(tx.enrollment_id),
        safe_spreadsheet_text(tx.student_code_snapshot),
        tx.occurred_at.astimezone(timezone.utc).isoformat(),
        tx.occurred_at.astimezone(zone).isoformat(),
        safe_spreadsheet_text(timezone_name),
        safe_spreadsheet_text(tx.transaction_type),
        safe_spreadsheet_text(tx.source),
        tx.amount,
        tx.balance_before,
        tx.balance_after,
        safe_spreadsheet_text(tx.manual_category),
        safe_spreadsheet_text(tx.note),
        str(tx.action_id) if tx.action_id else "",
        safe_spreadsheet_text(tx.action_code_snapshot),
        safe_spreadsheet_text(tx.action_name_snapshot),
        safe_spreadsheet_text(tx.action_kind_snapshot),
        str(tx.quick_action_id) if tx.quick_action_id else "",
        safe_spreadsheet_text(tx.quick_action_code_snapshot),
        tx.adjustment_target_balance if tx.adjustment_target_balance is not None else "",
        str(tx.reverses_transaction_id) if tx.reverses_transaction_id else "",
        str(tx.financial_request_id),
        str(tx.financial_batch_id) if tx.financial_batch_id else "",
        str(tx.actor_user_id) if tx.actor_user_id else "",
    )


def iter_classroom_transaction_csv(
    db: Session, selection: TransactionHistorySelection,
) -> Iterator[str]:
    """Stream every matching posted ledger entry, not just the HTML page."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    zone = ZoneInfo(selection.timezone_name)
    query = selection.statement.order_by(FinancialTransaction.display_number.desc())
    for tx in db.scalars(query.execution_options(stream_results=True)).yield_per(250):
        writer.writerow(_transaction_cells(tx, zone, selection.timezone_name))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
