from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.classrooms import ClassroomAccess, require_classroom_access


HISTORY_PAGE_SIZE = 50
TRANSACTION_TYPES = (
    "opening_balance", "action", "manual", "adjustment", "reversal",
    "allowance", "job_wage", "reward_purchase", "bulk",
)
TRANSACTION_SOURCES = (
    "scanner", "phone", "manual", "automatic",
    "reward_store", "admin", "bulk", "system",
)
MANUAL_CATEGORIES = ("general", "behavior_positive", "behavior_negative")


class TransactionHistoryValidationError(ValueError):
    pass


class TransactionHistoryEnrollmentNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TransactionHistoryRow:
    transaction: FinancialTransaction
    occurred_local: datetime
    reverses_number: int | None
    reversed_by_number: int | None


@dataclass(frozen=True)
class TransactionHistorySelection:
    access: ClassroomAccess
    roster: list[tuple[Student, Enrollment]]
    statement: Select[tuple[FinancialTransaction]]
    timezone_name: str


@dataclass(frozen=True)
class TransactionHistoryPage:
    access: ClassroomAccess
    roster: list[tuple[Student, Enrollment]]
    rows: list[TransactionHistoryRow]
    next_before: int | None
    timezone_name: str


def select_classroom_transactions(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID | None = None,
    transaction_type: str = "",
    source: str = "",
    category: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> TransactionHistorySelection:
    """Authorize and build the shared, classroom-scoped ledger filter query."""
    access = require_classroom_access(
        db, user_id=user_id, classroom_id=classroom_id,
    )
    if transaction_type and transaction_type not in TRANSACTION_TYPES:
        raise TransactionHistoryValidationError("invalid transaction type filter")
    if source and source not in TRANSACTION_SOURCES:
        raise TransactionHistoryValidationError("invalid transaction source filter")
    if category and category not in MANUAL_CATEGORIES:
        raise TransactionHistoryValidationError("invalid manual category filter")
    if date_from and date_to and date_from > date_to:
        raise TransactionHistoryValidationError("start date must be on or before end date")

    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise TransactionHistoryValidationError("classroom settings unavailable")
    zone = ZoneInfo(settings.timezone)

    roster = list(db.execute(
        select(Student, Enrollment)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .where(Enrollment.classroom_id == classroom_id)
        .order_by(Enrollment.student_code)
    ).all())

    if enrollment_id is not None and not any(
        enrollment.id == enrollment_id for _, enrollment in roster
    ):
        raise TransactionHistoryEnrollmentNotFoundError("enrollment not found in classroom")

    stmt = select(FinancialTransaction).where(
        FinancialTransaction.classroom_id == classroom_id,
    )
    if enrollment_id is not None:
        stmt = stmt.where(FinancialTransaction.enrollment_id == enrollment_id)
    if transaction_type:
        stmt = stmt.where(FinancialTransaction.transaction_type == transaction_type)
    if source:
        stmt = stmt.where(FinancialTransaction.source == source)
    if category:
        stmt = stmt.where(FinancialTransaction.manual_category == category)
    if date_from is not None:
        lower = datetime.combine(date_from, time.min, tzinfo=zone).astimezone(timezone.utc)
        stmt = stmt.where(FinancialTransaction.occurred_at >= lower)
    if date_to is not None:
        try:
            next_day = date_to + timedelta(days=1)
        except OverflowError as exc:
            raise TransactionHistoryValidationError("end date is outside supported range") from exc
        upper = datetime.combine(next_day, time.min, tzinfo=zone).astimezone(timezone.utc)
        stmt = stmt.where(FinancialTransaction.occurred_at < upper)

    return TransactionHistorySelection(
        access=access,
        roster=roster,
        statement=stmt,
        timezone_name=settings.timezone,
    )


def list_classroom_transactions(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID | None = None,
    transaction_type: str = "",
    source: str = "",
    category: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    before: int | None = None,
) -> TransactionHistoryPage:
    """Read-only, keyset-paged classroom ledger with all filters tenant-bound."""
    selection = select_classroom_transactions(
        db, user_id=user_id, classroom_id=classroom_id,
        enrollment_id=enrollment_id, transaction_type=transaction_type,
        source=source, category=category, date_from=date_from, date_to=date_to,
    )
    if before is not None and before <= 0:
        raise TransactionHistoryValidationError("invalid pagination cursor")
    stmt = selection.statement
    zone = ZoneInfo(selection.timezone_name)
    if before is not None:
        stmt = stmt.where(FinancialTransaction.display_number < before)

    items = list(db.scalars(
        stmt.order_by(FinancialTransaction.display_number.desc())
        .limit(HISTORY_PAGE_SIZE + 1)
    ))
    has_more = len(items) > HISTORY_PAGE_SIZE
    visible = items[:HISTORY_PAGE_SIZE]
    next_before = visible[-1].display_number if visible and has_more else None

    page_ids = {tx.id for tx in visible}
    target_ids = {
        tx.reverses_transaction_id for tx in visible
        if tx.reverses_transaction_id is not None
    }
    related = list(db.scalars(
        select(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
            or_(
                FinancialTransaction.id.in_(target_ids),
                FinancialTransaction.reverses_transaction_id.in_(page_ids),
            ),
        )
    )) if visible else []
    linked = {tx.id: tx for tx in visible + related}
    reversed_by = {
        tx.reverses_transaction_id: tx.display_number
        for tx in visible + related
        if tx.reverses_transaction_id is not None
    }

    return TransactionHistoryPage(
        access=selection.access,
        roster=selection.roster,
        rows=[
            TransactionHistoryRow(
                transaction=tx,
                occurred_local=tx.occurred_at.astimezone(zone),
                reverses_number=(
                    linked[tx.reverses_transaction_id].display_number
                    if tx.reverses_transaction_id in linked else None
                ),
                reversed_by_number=reversed_by.get(tx.id),
            )
            for tx in visible
        ],
        next_before=next_before,
        timezone_name=selection.timezone_name,
    )
