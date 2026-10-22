from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import SessionLocal
from app.models.automation import AutomationRun
from app.models.classroom import Classroom, ClassroomSettings
from app.models.student import Enrollment
from app.services.financial import post_weekly_allowance


@dataclass(frozen=True)
class AllowancePeriod:
    key: str
    due_at: datetime
    local_due_date: date


@dataclass(frozen=True)
class AllowanceRunSummary:
    classrooms_due: int
    payments_posted: int
    payments_replayed: int
    payments_skipped: int
    payments_failed: int


def due_allowance_period(
    settings: ClassroomSettings, now: datetime,
) -> AllowancePeriod | None:
    """Resolve this week's allowance deadline in the classroom's IANA timezone."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")
    zone = ZoneInfo(settings.timezone)
    local_now = now.astimezone(zone)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    due_date = monday + timedelta(days=settings.weekly_allowance_day)
    due_at = datetime.combine(
        due_date, settings.weekly_allowance_time, tzinfo=zone,
    ).astimezone(timezone.utc)
    if now.astimezone(timezone.utc) < due_at:
        return None
    return AllowancePeriod(
        key=monday.isoformat(), due_at=due_at, local_due_date=due_date,
    )


def run_due_weekly_allowances(
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
    now: datetime | None = None,
    classroom_id: uuid.UUID | None = None,
) -> AllowanceRunSummary:
    """Execute in an external scheduled worker, never a FastAPI web worker.

    Each eligible enrollment is committed independently. Uncommitted failures
    leave no payment marker or ledger entry; a later run retries safely.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")

    with session_factory() as db:
        stmt = (
            select(Classroom.id, ClassroomSettings)
            .join(ClassroomSettings, ClassroomSettings.classroom_id == Classroom.id)
            .where(
                Classroom.archived_at.is_(None),
                ClassroomSettings.weekly_allowance_enabled.is_(True),
            )
            .order_by(Classroom.id)
        )
        if classroom_id is not None:
            stmt = stmt.where(Classroom.id == classroom_id)
        classrooms = list(db.execute(stmt))

    totals = {
        "classrooms_due": 0, "payments_posted": 0,
        "payments_replayed": 0, "payments_skipped": 0, "payments_failed": 0,
    }
    for active_classroom_id, settings in classrooms:
        period = due_allowance_period(settings, now)
        if period is None:
            continue
        totals["classrooms_due"] += 1

        with session_factory.begin() as db:
            run = AutomationRun(
                id=uuid.uuid4(), classroom_id=active_classroom_id,
                run_type="allowance", period_key=period.key, status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id

        with session_factory() as db:
            enrollment_ids = list(db.scalars(
                select(Enrollment.id).where(
                    Enrollment.classroom_id == active_classroom_id,
                    Enrollment.active.is_(True),
                    Enrollment.created_at <= period.due_at,
                    or_(
                        Enrollment.started_on.is_(None),
                        Enrollment.started_on <= period.local_due_date,
                    ),
                    or_(
                        Enrollment.ended_on.is_(None),
                        Enrollment.ended_on >= period.local_due_date,
                    ),
                ).order_by(Enrollment.id)
            ))

        posted = replayed = skipped = failed = 0
        first_error: str | None = None
        for enrollment_id in enrollment_ids:
            try:
                with session_factory.begin() as db:
                    result = post_weekly_allowance(
                        db, classroom_id=active_classroom_id,
                        enrollment_id=enrollment_id, period_key=period.key,
                        due_at=period.due_at, local_due_date=period.local_due_date,
                        automation_run_id=run_id,
                    )
                if result is None:
                    skipped += 1
                elif result.replayed:
                    replayed += 1
                else:
                    posted += 1
            except Exception as exc:
                # Per-student rollback retains no claim or ledger row; retry later.
                failed += 1
                if first_error is None:
                    first_error = type(exc).__name__

        with session_factory.begin() as db:
            run = db.get(AutomationRun, run_id)
            if run is None:
                raise RuntimeError("automation run disappeared")
            run.attempted_count = len(enrollment_ids)
            run.completed_count = posted + replayed
            run.rejected_count = failed
            run.error_summary = first_error
            run.status = (
                "completed" if not failed else
                "partial" if posted or replayed else "failed"
            )
            run.finished_at = datetime.now(timezone.utc)

        totals["payments_posted"] += posted
        totals["payments_replayed"] += replayed
        totals["payments_skipped"] += skipped
        totals["payments_failed"] += failed
    return AllowanceRunSummary(**totals)
