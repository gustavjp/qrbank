from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.behavior import BehaviorEvent, BehaviorReason
from app.services.classrooms import (
    ClassroomAccess, ClassroomPermissionError, require_classroom_access,
)


MANAGER_ROLES = {"owner", "teacher"}
REASON_TYPES = ("positive", "negative")
CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,32}$")


class BehaviorReasonNotFoundError(LookupError):
    pass


class BehaviorReasonValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReasonCatalog:
    access: ClassroomAccess
    reasons: list[BehaviorReason]


@dataclass(frozen=True)
class BehaviorEventDetail:
    access: ClassroomAccess
    event: BehaviorEvent
    choices: list[BehaviorReason]
    audits: list[AdminAuditEvent]


def _manager(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ClassroomAccess:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    if access.membership.role not in MANAGER_ROLES:
        raise ClassroomPermissionError("owner or teacher permission is required")
    return access


def _code(value: str) -> str:
    cleaned = value.strip().upper()
    if not CODE_PATTERN.fullmatch(cleaned):
        raise BehaviorReasonValidationError(
            "reason code must be 1-32 letters, numbers, underscores or hyphens"
        )
    return cleaned


def _name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 120:
        raise BehaviorReasonValidationError("reason name must be 1-120 characters")
    return cleaned


def _optional_text(value: str | None, limit: int, label: str) -> str | None:
    cleaned = value.strip() if value else ""
    if len(cleaned) > limit:
        raise BehaviorReasonValidationError(f"{label} must be at most {limit} characters")
    return cleaned or None


def _snapshot(reason: BehaviorReason) -> dict:
    return {
        "code": reason.reason_code,
        "name": reason.name,
        "type": reason.behavior_type,
        "description": reason.description,
        "active": reason.active,
    }


def _detail_snapshot(event: BehaviorEvent) -> dict:
    return {
        "reason_id": str(event.behavior_reason_id) if event.behavior_reason_id else None,
        "reason_code": event.reason_code_snapshot,
        "reason_name": event.reason_name_snapshot,
        "note": event.note,
    }


def _audit(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    event_type: str, target_type: str, target_id: uuid.UUID,
    before: dict | None, after: dict | None,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=classroom_id, actor_user_id=user_id,
        event_type=event_type, target_type=target_type, target_id=target_id,
        before_json=before, after_json=after,
    ))


def list_reasons(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ReasonCatalog:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    rows = list(db.scalars(select(BehaviorReason).where(
        BehaviorReason.classroom_id == classroom_id,
    ).order_by(BehaviorReason.behavior_type, BehaviorReason.reason_code)))
    return ReasonCatalog(access=access, reasons=rows)


def create_reason(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    code: str, name: str, behavior_type: str, description: str | None,
) -> BehaviorReason:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    if behavior_type not in REASON_TYPES:
        raise BehaviorReasonValidationError("invalid behavior type")
    reason = BehaviorReason(
        id=uuid.uuid4(), classroom_id=classroom_id, reason_code=_code(code),
        name=_name(name), behavior_type=behavior_type,
        description=_optional_text(description, 1000, "description"), active=True,
    )
    db.add(reason)
    db.flush()
    _audit(
        db, user_id=user_id, classroom_id=classroom_id,
        event_type="behavior_reason_created", target_type="behavior_reason",
        target_id=reason.id, before=None, after=_snapshot(reason),
    )
    db.flush()
    return reason


def update_reason(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    reason_id: uuid.UUID, code: str, name: str, description: str | None,
) -> BehaviorReason:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    reason = db.scalar(select(BehaviorReason).where(
        BehaviorReason.id == reason_id,
        BehaviorReason.classroom_id == classroom_id,
    ).with_for_update())
    if reason is None:
        raise BehaviorReasonNotFoundError("behavior reason not found")
    before = _snapshot(reason)
    reason.reason_code = _code(code)
    reason.name = _name(name)
    reason.description = _optional_text(description, 1000, "description")
    after = _snapshot(reason)
    if before != after:
        reason.updated_at = datetime.now(timezone.utc)
        _audit(
            db, user_id=user_id, classroom_id=classroom_id,
            event_type="behavior_reason_updated", target_type="behavior_reason",
            target_id=reason.id, before=before, after=after,
        )
    db.flush()
    return reason


def set_reason_active(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    reason_id: uuid.UUID, active: bool,
) -> BehaviorReason:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    reason = db.scalar(select(BehaviorReason).where(
        BehaviorReason.id == reason_id,
        BehaviorReason.classroom_id == classroom_id,
    ).with_for_update())
    if reason is None:
        raise BehaviorReasonNotFoundError("behavior reason not found")
    if reason.active != active:
        before = _snapshot(reason)
        reason.active = active
        reason.updated_at = datetime.now(timezone.utc)
        _audit(
            db, user_id=user_id, classroom_id=classroom_id,
            event_type="behavior_reason_reactivated" if active else "behavior_reason_deactivated",
            target_type="behavior_reason", target_id=reason.id,
            before=before, after=_snapshot(reason),
        )
    db.flush()
    return reason


def get_event_detail(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    event_id: uuid.UUID,
) -> BehaviorEventDetail:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    event = db.scalar(select(BehaviorEvent).where(
        BehaviorEvent.id == event_id, BehaviorEvent.classroom_id == classroom_id,
    ))
    if event is None:
        raise BehaviorReasonNotFoundError("behavior event not found")
    choices = list(db.scalars(select(BehaviorReason).where(
        BehaviorReason.classroom_id == classroom_id,
        BehaviorReason.behavior_type == event.behavior_type,
        or_(BehaviorReason.active.is_(True), BehaviorReason.id == event.behavior_reason_id),
    ).order_by(BehaviorReason.reason_code)))
    audits = list(db.scalars(select(AdminAuditEvent).where(
        AdminAuditEvent.classroom_id == classroom_id,
        AdminAuditEvent.target_type == "behavior_event",
        AdminAuditEvent.target_id == event_id,
        AdminAuditEvent.event_type == "behavior_detail_updated",
    ).order_by(AdminAuditEvent.occurred_at.desc(), AdminAuditEvent.id.desc()).limit(50)))
    return BehaviorEventDetail(access=access, event=event, choices=choices, audits=audits)


def update_event_detail(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    event_id: uuid.UUID, reason_id: uuid.UUID | None, note: str | None,
) -> BehaviorEvent:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    event = db.scalar(select(BehaviorEvent).where(
        BehaviorEvent.id == event_id, BehaviorEvent.classroom_id == classroom_id,
    ).with_for_update())
    if event is None:
        raise BehaviorReasonNotFoundError("behavior event not found")

    cleaned_note = _optional_text(note, 2000, "detail note")
    reason = None
    if reason_id is not None:
        reason = db.scalar(select(BehaviorReason).where(
            BehaviorReason.id == reason_id,
            BehaviorReason.classroom_id == classroom_id,
            BehaviorReason.behavior_type == event.behavior_type,
        ))
        if reason is None:
            raise BehaviorReasonValidationError(
                "reason must match this classroom and behavior type"
            )
        if not reason.active and reason.id != event.behavior_reason_id:
            raise BehaviorReasonValidationError("inactive reasons cannot be newly attached")

    before = _detail_snapshot(event)
    # Editing the note or saving again must not refresh an old reason
    # snapshot after its catalog name/code has changed.
    same_reason = reason is not None and reason.id == event.behavior_reason_id
    event.behavior_reason_id = reason.id if reason else None
    if not same_reason:
        event.reason_code_snapshot = reason.reason_code if reason else None
        event.reason_name_snapshot = reason.name if reason else None
    event.note = cleaned_note
    after = _detail_snapshot(event)
    if before != after:
        event.reason_attached_at = datetime.now(timezone.utc)
        event.reason_attached_by_user_id = user_id
        _audit(
            db, user_id=user_id, classroom_id=classroom_id,
            event_type="behavior_detail_updated", target_type="behavior_event",
            target_id=event.id, before=before, after=after,
        )
    db.flush()
    return event
