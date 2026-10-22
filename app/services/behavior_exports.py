from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_
from sqlalchemy.orm import Session, aliased

from app.models.behavior import BehaviorEvent
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.behavior_history import BehaviorHistorySelection
from app.services.csv_security import safe_spreadsheet_text


BEHAVIOR_CSV_COLUMNS = (
    "event_id", "classroom_id", "enrollment_id", "student_code",
    "student_name", "behavior_type", "occurred_at_utc",
    "occurred_at_classroom_local", "classroom_timezone",
    "action_id", "action_code_snapshot", "action_name_snapshot",
    "behavior_reason_id", "reason_code_snapshot", "reason_name_snapshot",
    "event_note", "reason_attached_at_utc", "reason_attached_by_user_id",
    "created_by_user_id", "financial_transaction_id",
    "financial_display_number", "points_amount", "financial_source",
    "financial_note", "reversed_by_transaction_id",
    "reversed_by_display_number",
)


def iter_classroom_behavior_csv(
    db: Session, selection: BehaviorHistorySelection,
) -> Iterator[str]:
    """Stream all matching behavior events from one read-only PostgreSQL snapshot."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(BEHAVIOR_CSV_COLUMNS)
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    zone = ZoneInfo(selection.timezone_name)
    reversal = aliased(FinancialTransaction)
    # Join each related row through the authorized classroom, never by ID alone.
    query = (
        selection.statement
        .add_columns(Enrollment, Student, FinancialTransaction, reversal)
        .join(Enrollment, and_(
            Enrollment.id == BehaviorEvent.enrollment_id,
            Enrollment.classroom_id == selection.access.classroom.id,
        ))
        .join(Student, Student.id == Enrollment.student_id)
        .outerjoin(FinancialTransaction, and_(
            FinancialTransaction.id == BehaviorEvent.financial_transaction_id,
            FinancialTransaction.classroom_id == selection.access.classroom.id,
            FinancialTransaction.enrollment_id == BehaviorEvent.enrollment_id,
        ))
        .outerjoin(reversal, and_(
            reversal.reverses_transaction_id == FinancialTransaction.id,
            reversal.classroom_id == selection.access.classroom.id,
            reversal.enrollment_id == BehaviorEvent.enrollment_id,
        ))
        .order_by(BehaviorEvent.occurred_at.desc(), BehaviorEvent.id.desc())
    )
    for event, enrollment, student, tx, reversed_by in db.execute(
        query.execution_options(stream_results=True)
    ).yield_per(250):
        code = tx.student_code_snapshot if tx is not None else enrollment.student_code
        writer.writerow((
            str(event.id),
            str(event.classroom_id),
            str(event.enrollment_id),
            safe_spreadsheet_text(code),
            safe_spreadsheet_text(student.display_name),
            safe_spreadsheet_text(event.behavior_type),
            event.occurred_at.astimezone(timezone.utc).isoformat(),
            event.occurred_at.astimezone(zone).isoformat(),
            safe_spreadsheet_text(selection.timezone_name),
            str(event.action_id) if event.action_id else "",
            safe_spreadsheet_text(event.action_code_snapshot),
            safe_spreadsheet_text(event.action_name_snapshot),
            str(event.behavior_reason_id) if event.behavior_reason_id else "",
            safe_spreadsheet_text(event.reason_code_snapshot),
            safe_spreadsheet_text(event.reason_name_snapshot),
            safe_spreadsheet_text(event.note),
            (
                event.reason_attached_at.astimezone(timezone.utc).isoformat()
                if event.reason_attached_at else ""
            ),
            str(event.reason_attached_by_user_id) if event.reason_attached_by_user_id else "",
            str(event.created_by_user_id) if event.created_by_user_id else "",
            str(tx.id) if tx else "",
            tx.display_number if tx else "",
            tx.amount if tx else "",
            safe_spreadsheet_text(tx.source) if tx else "",
            safe_spreadsheet_text(tx.note) if tx else "",
            str(reversed_by.id) if reversed_by else "",
            reversed_by.display_number if reversed_by else "",
        ))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
