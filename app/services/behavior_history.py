from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.classrooms import ClassroomAccess
from app.services.students import select_classroom_enrollments


BEHAVIOR_PAGE_SIZE = 50
BEHAVIOR_TYPES = ("positive", "negative")


class BehaviorHistoryValidationError(ValueError):
    pass


class BehaviorHistoryEnrollmentNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class BehaviorHistoryRow:
    event: BehaviorEvent
    transaction: FinancialTransaction | None
    student_code: str
    student_name: str | None
    occurred_local: datetime
    reversed_by_number: int | None


@dataclass(frozen=True)
class BehaviorHistoryPage:
    access: ClassroomAccess
    roster: list[tuple[Student, Enrollment]]
    rows: list[BehaviorHistoryRow]
    positive_count: int
    negative_count: int
    next_before: str | None
    timezone_name: str


def _read_cursor(raw: str) -> tuple[datetime, uuid.UUID] | None:
    if not raw:
        return None
    if len(raw) > 100 or raw.count("|") != 1:
        raise BehaviorHistoryValidationError("invalid pagination cursor")
    timestamp_text, event_id_text = raw.split("|")
    try:
        occurred_at = datetime.fromisoformat(timestamp_text)
        event_id = uuid.UUID(event_id_text)
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        return occurred_at.astimezone(timezone.utc), event_id
    except (ValueError, OverflowError) as exc:
        raise BehaviorHistoryValidationError("invalid pagination cursor") from exc


def list_classroom_behavior(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID | None = None,
    behavior_type: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    before: str = "",
) -> BehaviorHistoryPage:
    """Read-only, membership-scoped behavior counts and stable event pagination."""
    selection = select_classroom_enrollments(
        db, user_id=user_id, classroom_id=classroom_id,
    )
    if behavior_type and behavior_type not in BEHAVIOR_TYPES:
        raise BehaviorHistoryValidationError("invalid behavior type filter")
    if date_from and date_to and date_from > date_to:
        raise BehaviorHistoryValidationError("start date must be on or before end date")
    cursor = _read_cursor(before)
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise BehaviorHistoryValidationError("classroom settings unavailable")
    zone = ZoneInfo(settings.timezone)

    roster = list(db.execute(selection.statement).all())
    if enrollment_id is not None and not any(
        enrollment.id == enrollment_id for _, enrollment in roster
    ):
        raise BehaviorHistoryEnrollmentNotFoundError("enrollment not found in classroom")

    stmt = select(BehaviorEvent).where(BehaviorEvent.classroom_id == classroom_id)
    if enrollment_id is not None:
        stmt = stmt.where(BehaviorEvent.enrollment_id == enrollment_id)
    if behavior_type:
        stmt = stmt.where(BehaviorEvent.behavior_type == behavior_type)
    if date_from is not None:
        lower = datetime.combine(date_from, time.min, tzinfo=zone).astimezone(timezone.utc)
        stmt = stmt.where(BehaviorEvent.occurred_at >= lower)
    if date_to is not None:
        try:
            next_day = date_to + timedelta(days=1)
        except OverflowError as exc:
            raise BehaviorHistoryValidationError("end date is outside supported range") from exc
        upper = datetime.combine(next_day, time.min, tzinfo=zone).astimezone(timezone.utc)
        stmt = stmt.where(BehaviorEvent.occurred_at < upper)

    # Report totals over all filtered records, not just the visible 50.
    count_rows = db.execute(
        stmt.with_only_columns(BehaviorEvent.behavior_type, func.count())
        .group_by(BehaviorEvent.behavior_type)
    ).all()
    totals = {kind: count for kind, count in count_rows}

    page_stmt = stmt
    if cursor is not None:
        occurred_at, event_id = cursor
        page_stmt = page_stmt.where(or_(
            BehaviorEvent.occurred_at < occurred_at,
            and_(
                BehaviorEvent.occurred_at == occurred_at,
                BehaviorEvent.id < event_id,
            ),
        ))
    items = list(db.scalars(
        page_stmt.order_by(BehaviorEvent.occurred_at.desc(), BehaviorEvent.id.desc())
        .limit(BEHAVIOR_PAGE_SIZE + 1)
    ))
    visible = items[:BEHAVIOR_PAGE_SIZE]
    next_before = None
    if len(items) > BEHAVIOR_PAGE_SIZE:
        last = visible[-1]
        next_before = (
            f"{last.occurred_at.astimezone(timezone.utc).isoformat()}|{last.id}"
        )

    if not visible:
        return BehaviorHistoryPage(
            access=selection.access, roster=roster, rows=[],
            positive_count=totals.get("positive", 0),
            negative_count=totals.get("negative", 0),
            next_before=None, timezone_name=settings.timezone,
        )

    # Explicit tenant equality on every join keeps event, enrollment and
    # linked ledger rows inside the authorized classroom.
    reversal = aliased(FinancialTransaction)
    details = db.execute(
        select(BehaviorEvent, Enrollment, Student, FinancialTransaction, reversal)
        .join(Enrollment, and_(
            Enrollment.id == BehaviorEvent.enrollment_id,
            Enrollment.classroom_id == classroom_id,
        ))
        .join(Student, Student.id == Enrollment.student_id)
        .outerjoin(FinancialTransaction, and_(
            FinancialTransaction.id == BehaviorEvent.financial_transaction_id,
            FinancialTransaction.classroom_id == classroom_id,
            FinancialTransaction.enrollment_id == BehaviorEvent.enrollment_id,
        ))
        .outerjoin(reversal, and_(
            reversal.reverses_transaction_id == FinancialTransaction.id,
            reversal.classroom_id == classroom_id,
            reversal.enrollment_id == BehaviorEvent.enrollment_id,
        ))
        .where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.id.in_([event.id for event in visible]),
        )
    ).all()
    detail_by_id = {
        event.id: BehaviorHistoryRow(
            event=event,
            transaction=transaction,
            student_code=(
                transaction.student_code_snapshot
                if transaction is not None else enrollment.student_code
            ),
            student_name=student.display_name,
            occurred_local=event.occurred_at.astimezone(zone),
            reversed_by_number=(
                reverse_transaction.display_number
                if reverse_transaction is not None else None
            ),
        )
        for event, enrollment, student, transaction, reverse_transaction in details
    }

    return BehaviorHistoryPage(
        access=selection.access,
        roster=roster,
        rows=[detail_by_id[event.id] for event in visible],
        positive_count=totals.get("positive", 0),
        negative_count=totals.get("negative", 0),
        next_before=next_before,
        timezone_name=settings.timezone,
    )
