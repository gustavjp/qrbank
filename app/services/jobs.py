from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.job import Job
from app.services.classrooms import (
    ClassroomAccess,
    ClassroomPermissionError,
    require_classroom_access,
)


MANAGER_ROLES = {"owner", "teacher"}
JOB_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,32}$")
MAX_WAGE = 2**31 - 1


class JobValidationError(ValueError):
    pass


class JobNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class JobCatalog:
    access: ClassroomAccess
    jobs: list[Job]


def _code(value: str) -> str:
    code = value.strip().upper()
    if not JOB_CODE_PATTERN.fullmatch(code):
        raise JobValidationError(
            "job code must contain 1-32 letters, numbers, underscores, or hyphens"
        )
    return code


def _name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 120:
        raise JobValidationError("job name must contain 1-120 characters")
    return name


def _wage(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_WAGE:
        raise JobValidationError("weekly wage must be a nonnegative signed integer")
    return value


def _notes(value: str | None) -> str | None:
    notes = value.strip() if value else ""
    if len(notes) > 1000:
        raise JobValidationError("notes must contain at most 1000 characters")
    return notes or None


def _manager(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ClassroomAccess:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    if access.membership.role not in MANAGER_ROLES:
        raise ClassroomPermissionError("owner or teacher permission is required")
    return access


def _snapshot(job: Job) -> dict:
    return {
        "code": job.job_code,
        "name": job.name,
        "weekly_wage": job.weekly_wage,
        "notes": job.notes,
        "active": job.active,
    }


def _audit(
    db: Session, *, job: Job, user_id: uuid.UUID, event_type: str,
    before: dict | None, after: dict,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=job.classroom_id, actor_user_id=user_id,
        event_type=event_type, target_type="job", target_id=job.id,
        before_json=before, after_json=after,
    ))


def list_jobs(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> JobCatalog:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    jobs = list(db.scalars(
        select(Job).where(Job.classroom_id == classroom_id).order_by(Job.job_code)
    ))
    return JobCatalog(access=access, jobs=jobs)


def get_job(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, job_id: uuid.UUID,
) -> tuple[ClassroomAccess, Job]:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    job = db.scalar(select(Job).where(Job.id == job_id, Job.classroom_id == classroom_id))
    if job is None:
        raise JobNotFoundError("job not found")
    return access, job


def create_job(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    code: str, name: str, weekly_wage: int, notes: str | None,
) -> Job:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    job = Job(
        id=uuid.uuid4(), classroom_id=classroom_id,
        job_code=_code(code), name=_name(name),
        weekly_wage=_wage(weekly_wage), notes=_notes(notes), active=True,
    )
    db.add(job)
    db.flush()
    _audit(db, job=job, user_id=user_id, event_type="job_created",
           before=None, after=_snapshot(job))
    db.flush()
    return job


def update_job(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, job_id: uuid.UUID,
    code: str, name: str, weekly_wage: int, notes: str | None,
) -> Job:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    job = db.scalar(select(Job).where(
        Job.id == job_id, Job.classroom_id == classroom_id,
    ).with_for_update())
    if job is None:
        raise JobNotFoundError("job not found")

    # Validate all user-controlled values before mutating a tracked ORM object.
    new_code = _code(code)
    new_name = _name(name)
    new_wage = _wage(weekly_wage)
    new_notes = _notes(notes)
    before = _snapshot(job)
    job.job_code = new_code
    job.name = new_name
    job.weekly_wage = new_wage
    job.notes = new_notes
    after = _snapshot(job)
    if before != after:
        job.updated_at = datetime.now(timezone.utc)
        _audit(db, job=job, user_id=user_id, event_type="job_updated",
               before=before, after=after)
    db.flush()
    return job


def set_job_active(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, job_id: uuid.UUID,
    active: bool,
) -> Job:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    job = db.scalar(select(Job).where(
        Job.id == job_id, Job.classroom_id == classroom_id,
    ).with_for_update())
    if job is None:
        raise JobNotFoundError("job not found")
    if type(active) is not bool:
        raise JobValidationError("active must be true or false")
    if job.active != active:
        before = _snapshot(job)
        job.active = active
        job.updated_at = datetime.now(timezone.utc)
        _audit(db, job=job, user_id=user_id,
               event_type="job_reactivated" if active else "job_deactivated",
               before=before, after=_snapshot(job))
    db.flush()
    return job
