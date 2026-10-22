from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.classroom import ClassroomSettings
from app.models.job import Job, JobAssignment
from app.models.student import Enrollment, Student
from app.services.classrooms import (
    ClassroomAccess,
    ClassroomNotFoundError,
    ClassroomPermissionError,
    require_classroom_access,
)
from app.services.jobs import MANAGER_ROLES


class JobAssignmentNotFoundError(LookupError):
    pass


class JobAssignmentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AssignmentRow:
    assignment: JobAssignment
    job: Job
    enrollment: Enrollment
    student: Student


@dataclass(frozen=True)
class AssignmentCatalog:
    access: ClassroomAccess
    assignments: list[AssignmentRow]
    active_enrollments: list[tuple[Student, Enrollment]]
    active_jobs: list[Job]
    selected_enrollment_id: uuid.UUID | None


def _manager(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ClassroomAccess:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    if access.membership.role not in MANAGER_ROLES:
        raise ClassroomPermissionError("owner or teacher permission is required")
    return access


def _classroom_today(
    db: Session, classroom_id: uuid.UUID, *, now: datetime | None = None,
) -> date:
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise ClassroomNotFoundError("classroom settings not found")
    zone = ZoneInfo(settings.timezone)
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise JobAssignmentValidationError("clock must include a timezone")
        return now.astimezone(zone).date()
    return datetime.now(zone).date()


def _snapshot(assignment: JobAssignment) -> dict:
    return {
        "enrollment_id": str(assignment.enrollment_id),
        "job_id": str(assignment.job_id),
        "started_on": assignment.started_on.isoformat(),
        "ended_on": assignment.ended_on.isoformat() if assignment.ended_on else None,
        "ended_by_user_id": str(assignment.ended_by_user_id) if assignment.ended_by_user_id else None,
    }


def _audit(
    db: Session, *, assignment: JobAssignment, actor_id: uuid.UUID,
    event_type: str, before: dict | None, after: dict,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=assignment.classroom_id, actor_user_id=actor_id,
        event_type=event_type, target_type="job_assignment", target_id=assignment.id,
        before_json=before, after_json=after,
    ))


def list_job_assignments(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID | None = None,
) -> AssignmentCatalog:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)

    if enrollment_id is not None:
        enrollment_exists = db.scalar(select(Enrollment.id).where(
            Enrollment.id == enrollment_id, Enrollment.classroom_id == classroom_id,
        ))
        if enrollment_exists is None:
            raise JobAssignmentNotFoundError("student enrollment not found")

    stmt = (
        select(JobAssignment, Job, Enrollment, Student)
        .join(Job, Job.id == JobAssignment.job_id)
        .join(Enrollment, Enrollment.id == JobAssignment.enrollment_id)
        .join(Student, Student.id == Enrollment.student_id)
        .where(
            JobAssignment.classroom_id == classroom_id,
            Job.classroom_id == classroom_id,
            Enrollment.classroom_id == classroom_id,
        )
        .order_by(
            JobAssignment.ended_on.is_(None).desc(),
            JobAssignment.started_on.desc(),
            Enrollment.student_code,
            Job.job_code,
            JobAssignment.id,
        )
    )
    if enrollment_id is not None:
        stmt = stmt.where(JobAssignment.enrollment_id == enrollment_id)
    assignments = [
        AssignmentRow(assignment=a, job=j, enrollment=e, student=s)
        for a, j, e, s in db.execute(stmt)
    ]

    active_enrollments = list(db.execute(
        select(Student, Enrollment)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .where(Enrollment.classroom_id == classroom_id, Enrollment.active.is_(True))
        .order_by(Enrollment.student_code, Enrollment.id)
    ))
    active_jobs = list(db.scalars(
        select(Job).where(Job.classroom_id == classroom_id, Job.active.is_(True))
        .order_by(Job.job_code)
    ))
    return AssignmentCatalog(
        access=access, assignments=assignments,
        active_enrollments=active_enrollments, active_jobs=active_jobs,
        selected_enrollment_id=enrollment_id,
    )


def create_job_assignment(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID, job_id: uuid.UUID,
    now: datetime | None = None,
) -> JobAssignment:
    _manager(db, user_id=user_id, classroom_id=classroom_id)

    # Lock both status-bearing rows: deactivation and assignment cannot race.
    enrollment = db.scalar(select(Enrollment).where(
        Enrollment.id == enrollment_id, Enrollment.classroom_id == classroom_id,
    ).with_for_update())
    if enrollment is None:
        raise JobAssignmentNotFoundError("student enrollment not found")
    if not enrollment.active:
        raise JobAssignmentValidationError("cannot assign a job to an inactive student")

    job = db.scalar(select(Job).where(
        Job.id == job_id, Job.classroom_id == classroom_id,
    ).with_for_update())
    if job is None:
        raise JobAssignmentNotFoundError("classroom job not found")
    if not job.active:
        raise JobAssignmentValidationError("cannot assign an inactive job")

    started_on = _classroom_today(db, classroom_id, now=now)
    assignment = JobAssignment(
        id=uuid.uuid4(), classroom_id=classroom_id,
        enrollment_id=enrollment.id, job_id=job.id,
        started_on=started_on, ended_on=None,
    )
    db.add(assignment)
    # PostgreSQL's partial unique index rejects concurrent duplicate active pairs.
    db.flush()
    _audit(db, assignment=assignment, actor_id=user_id,
           event_type="job_assignment_created", before=None,
           after=_snapshot(assignment))
    db.flush()
    return assignment


def end_job_assignment(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    assignment_id: uuid.UUID, now: datetime | None = None,
) -> JobAssignment:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    assignment = db.scalar(select(JobAssignment).where(
        JobAssignment.id == assignment_id,
        JobAssignment.classroom_id == classroom_id,
    ).with_for_update())
    if assignment is None:
        raise JobAssignmentNotFoundError("job assignment not found")
    if assignment.ended_on is not None:
        return assignment

    ended_on = _classroom_today(db, classroom_id, now=now)
    if ended_on < assignment.started_on:
        raise JobAssignmentValidationError(
            "assignment cannot end before its classroom-local start date"
        )
    before = _snapshot(assignment)
    assignment.ended_on = ended_on
    assignment.ended_by_user_id = user_id
    _audit(db, assignment=assignment, actor_id=user_id,
           event_type="job_assignment_ended", before=before,
           after=_snapshot(assignment))
    db.flush()
    return assignment
