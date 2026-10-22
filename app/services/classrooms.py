from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings


class ClassroomNotFoundError(LookupError):
    pass


class ClassroomPermissionError(PermissionError):
    pass


class ClassroomValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ClassroomAccess:
    classroom: Classroom
    membership: ClassroomMembership


def _clean_required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ClassroomValidationError(f"{field_name} is required")
    return cleaned


def _validate_timezone(value: str) -> str:
    cleaned = _clean_required(value, "timezone")
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise ClassroomValidationError("timezone must be a valid IANA timezone") from exc
    return cleaned




def _configuration_snapshot(
    classroom: Classroom,
    settings: ClassroomSettings,
) -> dict[str, object]:
    return {
        "name": classroom.name,
        "school_year": classroom.school_year,
        "timezone": settings.timezone,
        "starting_balance": settings.starting_balance,
        "weekly_allowance_enabled": settings.weekly_allowance_enabled,
        "weekly_allowance_amount": settings.weekly_allowance_amount,
        "weekly_allowance_day": settings.weekly_allowance_day,
        "weekly_allowance_time": settings.weekly_allowance_time.isoformat(),
        "weekly_wages_enabled": settings.weekly_wages_enabled,
        "weekly_wage_day": settings.weekly_wage_day,
        "weekly_wage_time": settings.weekly_wage_time.isoformat(),
        "sound_enabled": settings.sound_enabled,
        "duplicate_scan_seconds": settings.duplicate_scan_seconds,
        "allow_negative_balance": settings.allow_negative_balance,
        "student_selection_timeout_seconds": settings.student_selection_timeout_seconds,
        "confirm_normal_transactions": settings.confirm_normal_transactions,
        "confirm_balance_reset": settings.confirm_balance_reset,
    }


def list_accessible_classrooms(
    db: Session,
    *,
    user_id: uuid.UUID,
) -> list[ClassroomAccess]:
    rows = db.execute(
        select(Classroom, ClassroomMembership)
        .join(
            ClassroomMembership,
            ClassroomMembership.classroom_id == Classroom.id,
        )
        .where(
            ClassroomMembership.user_id == user_id,
            ClassroomMembership.ended_at.is_(None),
            Classroom.archived_at.is_(None),
        )
        .order_by(Classroom.name, Classroom.school_year)
    ).all()
    return [
        ClassroomAccess(classroom=classroom, membership=membership)
        for classroom, membership in rows
    ]


def require_classroom_access(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
) -> ClassroomAccess:
    row = db.execute(
        select(Classroom, ClassroomMembership)
        .join(
            ClassroomMembership,
            ClassroomMembership.classroom_id == Classroom.id,
        )
        .where(
            Classroom.id == classroom_id,
            Classroom.archived_at.is_(None),
            ClassroomMembership.user_id == user_id,
            ClassroomMembership.ended_at.is_(None),
        )
    ).one_or_none()

    if row is None:
        raise ClassroomNotFoundError("classroom not found")

    classroom, membership = row
    return ClassroomAccess(classroom=classroom, membership=membership)


def require_classroom_owner(
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
    if access.membership.role != "owner":
        raise ClassroomPermissionError("owner permission is required")
    return access


def create_classroom_for_owner(
    db: Session,
    *,
    owner_user_id: uuid.UUID,
    name: str,
    school_year: str,
) -> Classroom:
    classroom = Classroom(
        id=uuid.uuid4(),
        name=_clean_required(name, "classroom name"),
        school_year=_clean_required(school_year, "school year"),
    )
    db.add(classroom)
    db.flush()

    db.add_all(
        [
            ClassroomMembership(
                id=uuid.uuid4(),
                classroom_id=classroom.id,
                user_id=owner_user_id,
                role="owner",
                created_by_user_id=owner_user_id,
            ),
            ClassroomSettings(classroom_id=classroom.id),
        ]
    )
    db.flush()
    return classroom


def get_classroom_settings(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
) -> tuple[ClassroomAccess, ClassroomSettings]:
    access = require_classroom_access(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise ClassroomNotFoundError("classroom settings not found")
    return access, settings


def update_classroom_configuration_for_owner(
    db: Session,
    *,
    user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    name: str,
    school_year: str,
    timezone: str,
    starting_balance: int,
    weekly_allowance_enabled: bool,
    weekly_allowance_amount: int,
    weekly_allowance_day: int,
    weekly_allowance_time: time,
    weekly_wages_enabled: bool,
    weekly_wage_day: int,
    weekly_wage_time: time,
    sound_enabled: bool,
    duplicate_scan_seconds: int,
    allow_negative_balance: bool,
    student_selection_timeout_seconds: int,
    confirm_normal_transactions: bool,
    confirm_balance_reset: bool,
) -> tuple[Classroom, ClassroomSettings]:
    access = require_classroom_owner(
        db,
        user_id=user_id,
        classroom_id=classroom_id,
    )
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise ClassroomNotFoundError("classroom settings not found")

    if not 0 <= weekly_allowance_day <= 6:
        raise ClassroomValidationError("allowance day must be between 0 and 6")
    if not 0 <= weekly_wage_day <= 6:
        raise ClassroomValidationError("wage day must be between 0 and 6")
    if duplicate_scan_seconds < 0:
        raise ClassroomValidationError("duplicate scan seconds cannot be negative")
    if student_selection_timeout_seconds <= 0:
        raise ClassroomValidationError("student selection timeout must be positive")
    if starting_balance < 0 and not allow_negative_balance:
        raise ClassroomValidationError(
            "starting balance cannot be negative while negative balances are disabled"
        )

    before_snapshot = _configuration_snapshot(access.classroom, settings)

    access.classroom.name = _clean_required(name, "classroom name")
    access.classroom.school_year = _clean_required(school_year, "school year")

    settings.timezone = _validate_timezone(timezone)
    settings.starting_balance = starting_balance
    settings.weekly_allowance_enabled = weekly_allowance_enabled
    settings.weekly_allowance_amount = weekly_allowance_amount
    settings.weekly_allowance_day = weekly_allowance_day
    settings.weekly_allowance_time = weekly_allowance_time
    settings.weekly_wages_enabled = weekly_wages_enabled
    settings.weekly_wage_day = weekly_wage_day
    settings.weekly_wage_time = weekly_wage_time
    settings.sound_enabled = sound_enabled
    settings.duplicate_scan_seconds = duplicate_scan_seconds
    settings.allow_negative_balance = allow_negative_balance
    settings.student_selection_timeout_seconds = student_selection_timeout_seconds
    settings.confirm_normal_transactions = confirm_normal_transactions
    settings.confirm_balance_reset = confirm_balance_reset
    settings.updated_by_user_id = user_id

    after_snapshot = _configuration_snapshot(access.classroom, settings)
    db.add(
        AdminAuditEvent(
            id=uuid.uuid4(),
            classroom_id=classroom_id,
            actor_user_id=user_id,
            event_type="classroom_settings_updated",
            target_type="classroom",
            target_id=classroom_id,
            before_json=before_snapshot,
            after_json=after_snapshot,
        )
    )

    db.flush()
    return access.classroom, settings
