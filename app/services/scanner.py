from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.action import Action, QuickAction
from app.models.student import Enrollment
from app.services.classrooms import ClassroomAccess, require_classroom_access


SCAN_CODE = re.compile(r"^[A-Z0-9_-]{1,32}$")


class ScanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ScanResolution:
    command: str
    code: str
    enrollment: Enrollment
    action: Action | None = None
    quick_action: QuickAction | None = None


def parse_scan_command(value: str) -> tuple[str, str]:
    parts = value.strip().upper().split(":")
    if len(parts) != 2 or parts[0] not in {"STUDENT", "QUICK", "ACTION"}:
        raise ScanValidationError("expected STUDENT:01, QUICK:QA1, or ACTION:A01")
    command, code = parts
    if not SCAN_CODE.fullmatch(code):
        raise ScanValidationError("scan code is invalid")
    return command, code


def _active_enrollment(
    db: Session, *, classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
) -> Enrollment:
    enrollment = db.scalar(select(Enrollment).where(
        Enrollment.classroom_id == classroom_id,
        Enrollment.id == enrollment_id,
        Enrollment.active.is_(True),
    ))
    if enrollment is None:
        raise ScanValidationError("selected student is not active in this classroom")
    return enrollment


def resolve_scan(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    payload: str, selected_enrollment_id: uuid.UUID | None = None,
) -> tuple[ClassroomAccess, ScanResolution]:
    """Resolve generic classroom-scoped QR commands, without moving points.

    The financial execution milestone will consume validated UUIDs, re-check
    classroom membership, and post through the central financial service.
    """
    access = require_classroom_access(
        db, user_id=user_id, classroom_id=classroom_id,
    )
    command, code = parse_scan_command(payload)

    if command == "STUDENT":
        enrollment = db.scalar(select(Enrollment).where(
            Enrollment.classroom_id == classroom_id,
            Enrollment.student_code == code,
            Enrollment.active.is_(True),
        ))
        if enrollment is None:
            raise ScanValidationError("student code is unknown or inactive in this classroom")
        return access, ScanResolution(command=command, code=code, enrollment=enrollment)

    if selected_enrollment_id is None:
        raise ScanValidationError("scan a student before scanning a Quick Action or Action")
    enrollment = _active_enrollment(
        db, classroom_id=classroom_id, enrollment_id=selected_enrollment_id,
    )

    if command == "ACTION":
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id,
            Action.action_code == code,
            Action.active.is_(True),
        ))
        if action is None:
            raise ScanValidationError("action code is unknown or inactive in this classroom")
        return access, ScanResolution(command=command, code=code,
                                      enrollment=enrollment, action=action)

    row = db.execute(
        select(QuickAction, Action)
        .join(Action, and_(
            QuickAction.action_id == Action.id,
            QuickAction.classroom_id == Action.classroom_id,
        ))
        .where(
            QuickAction.classroom_id == classroom_id,
            QuickAction.quick_action_code == code,
            QuickAction.active.is_(True),
            Action.active.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise ScanValidationError("Quick Action is unknown or inactive in this classroom")
    quick, action = row
    return access, ScanResolution(command=command, code=code,
                                  enrollment=enrollment, action=action,
                                  quick_action=quick)
