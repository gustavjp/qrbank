from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database import SessionLocal
from app.models.automation import AutomationRun
from app.models.classroom import Classroom, ClassroomSettings
from app.models.job import Job, JobAssignment
from app.models.student import Enrollment
from app.services.financial import post_weekly_job_wage


@dataclass(frozen=True)
class WagePeriod:
    key: str
    due_at: datetime
    local_due_date: date


@dataclass(frozen=True)
class WageRunSummary:
    classrooms_due: int
    payments_posted: int
    payments_replayed: int
    payments_skipped: int
    payments_failed: int


def due_wage_period(settings: ClassroomSettings, now: datetime) -> WagePeriod | None:
    """Resolve the current classroom-local week; never assume a fixed UTC offset."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    monday = local_now.date() - timedelta(days=local_now.weekday())
    due_date = monday + timedelta(days=settings.weekly_wage_day)
    due_local = datetime.combine(
        due_date, settings.weekly_wage_time, tzinfo=ZoneInfo(settings.timezone)
    )
    due_utc = due_local.astimezone(timezone.utc)
    if now.astimezone(timezone.utc) < due_utc:
        return None
    return WagePeriod(key=monday.isoformat(), due_at=due_utc, local_due_date=due_date)


def run_due_weekly_wages(
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
    now: datetime | None = None,
    classroom_id: uuid.UUID | None = None,
) -> WageRunSummary:
    """Run in a separate scheduled process, not inside FastAPI web workers.

    Each assignment is committed independently. Failures leave no partial
    financial transaction; later invocations safely retry unfinished payments.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")

    with session_factory() as db:
        stmt = (
            select(Classroom.id, ClassroomSettings)
            .join(ClassroomSettings, ClassroomSettings.classroom_id == Classroom.id)
            .where(Classroom.archived_at.is_(None),
                   ClassroomSettings.weekly_wages_enabled.is_(True))
            .order_by(Classroom.id)
        )
        if classroom_id is not None:
            stmt = stmt.where(Classroom.id == classroom_id)
        classrooms = list(db.execute(stmt))

    totals = {"classrooms_due": 0, "payments_posted": 0,
              "payments_replayed": 0, "payments_skipped": 0, "payments_failed": 0}
    for classroom_id, settings in classrooms:
        period = due_wage_period(settings, now)
        if period is None:
            continue
        totals["classrooms_due"] += 1

        with session_factory.begin() as db:
            run = AutomationRun(
                id=uuid.uuid4(), classroom_id=classroom_id,
                run_type="job_wages", period_key=period.key, status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id

        with session_factory() as db:
            assignment_ids = list(db.scalars(
                select(JobAssignment.id)
                .join(Job, Job.id == JobAssignment.job_id)
                .join(Enrollment, Enrollment.id == JobAssignment.enrollment_id)
                .where(
                    JobAssignment.classroom_id == classroom_id,
                    Job.classroom_id == classroom_id,
                    Enrollment.classroom_id == classroom_id,
                    Job.active.is_(True), Enrollment.active.is_(True),
                    JobAssignment.ended_on.is_(None),
                    JobAssignment.started_on <= period.local_due_date,
                    JobAssignment.created_at <= period.due_at,
                    Enrollment.created_at <= period.due_at,
                )
                .order_by(JobAssignment.enrollment_id, JobAssignment.id)
            ))

        posted = replayed = skipped = failed = 0
        first_error: str | None = None
        for assignment_id in assignment_ids:
            try:
                with session_factory.begin() as db:
                    result = post_weekly_job_wage(
                        db, classroom_id=classroom_id, assignment_id=assignment_id,
                        period_key=period.key, due_at=period.due_at,
                        local_due_date=period.local_due_date, automation_run_id=run_id,
                    )
                if result is None:
                    skipped += 1
                elif result.replayed:
                    replayed += 1
                else:
                    posted += 1
            except Exception as exc:
                # Never mark a failed payment complete. The failed transaction
                # rolls back; the next scheduler invocation can retry it.
                failed += 1
                if first_error is None:
                    first_error = type(exc).__name__

        with session_factory.begin() as db:
            run = db.get(AutomationRun, run_id)
            if run is None:
                raise RuntimeError("automation run disappeared")
            run.attempted_count = len(assignment_ids)
            run.completed_count = posted + replayed
            run.rejected_count = failed
            run.error_summary = first_error
            run.status = (
                "completed" if not failed else "partial" if posted or replayed else "failed"
            )
            run.finished_at = datetime.now(timezone.utc)

        totals["payments_posted"] += posted
        totals["payments_replayed"] += replayed
        totals["payments_skipped"] += skipped
        totals["payments_failed"] += failed
    return WageRunSummary(**totals)
