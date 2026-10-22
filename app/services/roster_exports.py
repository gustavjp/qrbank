from __future__ import annotations

import csv
import io
from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.services.csv_security import safe_spreadsheet_text
from app.services.students import EnrollmentSelection


ROSTER_CSV_COLUMNS = (
    "classroom_id", "enrollment_id", "student_id", "student_code",
    "display_name", "active", "current_balance", "started_on", "ended_on",
)


def iter_classroom_roster_csv(
    db: Session, selection: EnrollmentSelection,
) -> Iterator[str]:
    """Stream exact stored integer balances from one authorized classroom."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(ROSTER_CSV_COLUMNS)
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    # The response's read-only repeatable-read session gives every row the
    # same PostgreSQL snapshot, even while financial postings occur elsewhere.
    for student, enrollment in db.execute(
        selection.statement.execution_options(stream_results=True)
    ).yield_per(250):
        writer.writerow((
            str(enrollment.classroom_id),
            str(enrollment.id),
            str(student.id),
            safe_spreadsheet_text(enrollment.student_code),
            safe_spreadsheet_text(student.display_name),
            "true" if enrollment.active else "false",
            enrollment.current_balance,
            enrollment.started_on.isoformat() if enrollment.started_on else "",
            enrollment.ended_on.isoformat() if enrollment.ended_on else "",
        ))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
