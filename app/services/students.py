from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.classrooms import (
    ClassroomAccess,
    ClassroomPermissionError,
    require_classroom_access,
)
from app.services.financial import post_opening_balance


STUDENT_CODE_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")
STUDENT_MANAGER_ROLES = {"owner", "teacher"}


class StudentNotFoundError(LookupError):
    pass


class StudentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EnrollmentRecord:
    student: Student
    enrollment: Enrollment
    access: ClassroomAccess


def normalize_student_code(value: str) -> str:
    normalized = value.strip().upper()
    if not STUDENT_CODE_RE.fullmatch(normalized):
        raise StudentValidationError(
            "student code must use 1-32 letters, numbers, underscores, or hyphens"
        )
    return normalized


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _require_student_manager(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
) -> ClassroomAccess:
    access = require_classroom_access(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    if access.membership.role not in STUDENT_MANAGER_ROLES:
        raise ClassroomPermissionError(
            "owner or teacher permission is required"
        )
    return access


def list_enrollments(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    include_inactive: bool = True,
) -> tuple[ClassroomAccess, list[tuple[Student, Enrollment]]]:
    access = require_classroom_access(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )

    statement = (
        select(Student, Enrollment)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .where(Enrollment.classroom_id == classroom_id)
        .order_by(Enrollment.student_code)
    )
    if not include_inactive:
        statement = statement.where(Enrollment.active.is_(True))

    rows = db.execute(statement).all()
    return access, [(student, enrollment) for student, enrollment in rows]


def create_student_enrollment(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    student_code: str,
    display_name: str | None,
    notes: str | None,
) -> EnrollmentRecord:
    access = _require_student_manager(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    code = normalize_student_code(student_code)
    classroom_settings = db.get(ClassroomSettings, classroom_id)
    if classroom_settings is None:
        raise StudentNotFoundError("classroom settings not found")

    if (
        classroom_settings.starting_balance < 0
        and not classroom_settings.allow_negative_balance
    ):
        raise StudentValidationError(
            "classroom starting balance cannot be negative while negative balances are disabled"
        )

    student = Student(
        id=uuid.uuid4(),
        display_name=_clean_optional(display_name),
        created_by_user_id=user_id,
    )
    db.add(student)
    db.flush()

    enrollment = Enrollment(
        id=uuid.uuid4(),
        classroom_id=classroom_id,
        student_id=student.id,
        student_code=code,
        current_balance=0,
        notes=_clean_optional(notes),
        started_on=date.today(),
        active=True,
    )
    db.add(enrollment)
    db.flush()

    opening = None
    if classroom_settings.starting_balance != 0:
        opening = post_opening_balance(
            db,
            actor_user_id=user_id,
            classroom_id=classroom_id,
            enrollment_id=enrollment.id,
            amount=classroom_settings.starting_balance,
            student_code_snapshot=code,
        )

    db.add(
        AdminAuditEvent(
            id=uuid.uuid4(),
            classroom_id=classroom_id,
            actor_user_id=user_id,
            event_type="student_enrollment_created",
            target_type="enrollment",
            target_id=enrollment.id,
            before_json=None,
            after_json={
                "student_id": str(student.id),
                "student_code": code,
                "display_name": student.display_name,
                "active": True,
                "starting_balance": classroom_settings.starting_balance,
                "opening_transaction_id": (
                    str(opening.transaction.id) if opening is not None else None
                ),
            },
        )
    )
    db.flush()
    return EnrollmentRecord(
        student=student,
        enrollment=enrollment,
        access=access,
    )


def get_enrollment(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
) -> EnrollmentRecord:
    access = require_classroom_access(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    row = db.execute(
        select(Student, Enrollment)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
        )
    ).one_or_none()
    if row is None:
        raise StudentNotFoundError("student enrollment not found")

    student, enrollment = row
    return EnrollmentRecord(
        student=student,
        enrollment=enrollment,
        access=access,
    )


def update_student_enrollment(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    student_code: str,
    display_name: str | None,
    notes: str | None,
) -> EnrollmentRecord:
    access = _require_student_manager(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )

    enrollment = db.scalar(
        select(Enrollment)
        .where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
        )
        .with_for_update()
    )
    if enrollment is None:
        raise StudentNotFoundError("student enrollment not found")
    student = db.get(Student, enrollment.student_id)
    if student is None:
        raise StudentNotFoundError("student record not found")

    before = {
        "student_code": enrollment.student_code,
        "display_name": student.display_name,
        "notes": enrollment.notes,
    }

    enrollment.student_code = normalize_student_code(student_code)
    enrollment.notes = _clean_optional(notes)
    student.display_name = _clean_optional(display_name)

    after = {
        "student_code": enrollment.student_code,
        "display_name": student.display_name,
        "notes": enrollment.notes,
    }

    if before != after:
        db.add(
            AdminAuditEvent(
                id=uuid.uuid4(),
                classroom_id=classroom_id,
                actor_user_id=user_id,
                event_type="student_enrollment_updated",
                target_type="enrollment",
                target_id=enrollment.id,
                before_json=before,
                after_json=after,
            )
        )

    db.flush()
    return EnrollmentRecord(
        student=student,
        enrollment=enrollment,
        access=access,
    )


def set_enrollment_active(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    active: bool,
) -> EnrollmentRecord:
    access = _require_student_manager(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    enrollment = db.scalar(
        select(Enrollment)
        .where(
            Enrollment.id == enrollment_id,
            Enrollment.classroom_id == classroom_id,
        )
        .with_for_update()
    )
    if enrollment is None:
        raise StudentNotFoundError("student enrollment not found")
    student = db.get(Student, enrollment.student_id)
    if student is None:
        raise StudentNotFoundError("student record not found")

    if enrollment.active != active:
        before = {
            "active": enrollment.active,
            "ended_on": enrollment.ended_on.isoformat()
            if enrollment.ended_on is not None
            else None,
        }
        enrollment.active = active
        enrollment.ended_on = None if active else date.today()
        after = {
            "active": enrollment.active,
            "ended_on": enrollment.ended_on.isoformat()
            if enrollment.ended_on is not None
            else None,
        }
        db.add(
            AdminAuditEvent(
                id=uuid.uuid4(),
                classroom_id=classroom_id,
                actor_user_id=user_id,
                event_type=(
                    "student_enrollment_reactivated"
                    if active
                    else "student_enrollment_deactivated"
                ),
                target_type="enrollment",
                target_id=enrollment.id,
                before_json=before,
                after_json=after,
            )
        )

    db.flush()
    return EnrollmentRecord(
        student=student,
        enrollment=enrollment,
        access=access,
    )


def list_enrollment_transactions(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
) -> list[FinancialTransaction]:
    get_enrollment(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
        enrollment_id=enrollment_id,
    )
    return list(
        db.scalars(
            select(FinancialTransaction)
            .where(
                FinancialTransaction.classroom_id == classroom_id,
                FinancialTransaction.enrollment_id == enrollment_id,
            )
            .order_by(
                FinancialTransaction.occurred_at.desc(),
                FinancialTransaction.display_number.desc(),
            )
        )
    )
