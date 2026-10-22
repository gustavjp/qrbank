from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.action import Action, QuickAction
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom
from app.services.classrooms import (
    ClassroomAccess,
    ClassroomPermissionError,
    require_classroom_access,
)


CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,32}$")
ACTION_KINDS = {"general", "behavior_positive", "behavior_negative"}
MANAGER_ROLES = {"owner", "teacher"}
INTEGER_MIN = -(2**31 - 1)
INTEGER_MAX = 2**31 - 1

# The first-release Quick Action sheet described in SPEC.md.
# Codes are scoped to one classroom; initialization never replaces user edits.
DEFAULT_ACTIONS = (
    ("A01", "QA1", "General +5", "general", 5),
    ("A02", "QA2", "General +10", "general", 10),
    ("A03", "QA3", "General -5", "general", -5),
    ("A04", "QA4", "General -10", "general", -10),
    ("A05", "QA5", "Positive Behavior +5", "behavior_positive", 5),
    ("A06", "QA6", "Positive Behavior +10", "behavior_positive", 10),
    ("A07", "QA7", "Behavior Deduction -5", "behavior_negative", -5),
    ("A08", "QA8", "Behavior Deduction -10", "behavior_negative", -10),
    ("A09", "QA9", "General +1", "general", 1),
    ("A10", "QA10", "General -1", "general", -1),
)


class ActionValidationError(ValueError):
    pass


class ActionNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ActionCatalog:
    access: ClassroomAccess
    actions: list[Action]
    quick_actions: list[QuickAction]


def _normalize_code(value: str) -> str:
    code = value.strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise ActionValidationError(
            "code must contain 1-32 letters, numbers, underscores, or hyphens"
        )
    return code


def _name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ActionValidationError("action name is required")
    return name


def _description(value: str | None) -> str | None:
    cleaned = value.strip() if value else ""
    return cleaned or None


def _validate_amount(amount: int) -> int:
    if type(amount) is not int or not INTEGER_MIN <= amount <= INTEGER_MAX:
        raise ActionValidationError("amount must fit a signed integer and support an opposite reversal")
    return amount


def _validate_kind(kind: str) -> str:
    if kind not in ACTION_KINDS:
        raise ActionValidationError("invalid action category")
    return kind


def _validate_order(order: int) -> int:
    if type(order) is not int or not 0 <= order <= INTEGER_MAX:
        raise ActionValidationError("display order must be a nonnegative integer")
    return order


def _manager(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ClassroomAccess:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    if access.membership.role not in MANAGER_ROLES:
        raise ClassroomPermissionError("owner or teacher permission is required")
    return access


def _audit(
    db: Session,
    *,
    classroom_id: uuid.UUID,
    user_id: uuid.UUID,
    event_type: str,
    target_type: str,
    target_id: uuid.UUID,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=classroom_id, actor_user_id=user_id,
        event_type=event_type, target_type=target_type, target_id=target_id,
        before_json=before, after_json=after,
    ))


def _action_snapshot(action: Action) -> dict:
    return {
        "code": action.action_code,
        "name": action.name,
        "kind": action.action_kind,
        "amount": action.amount,
        "description": action.description,
        "active": action.active,
    }


def _quick_snapshot(quick: QuickAction) -> dict:
    return {
        "code": quick.quick_action_code,
        "action_id": str(quick.action_id),
        "active": quick.active,
        "display_order": quick.display_order,
    }


def list_catalog(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ActionCatalog:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    actions = list(db.scalars(
        select(Action).where(Action.classroom_id == classroom_id).order_by(Action.action_code)
    ))
    quick_actions = list(db.scalars(
        select(QuickAction).where(QuickAction.classroom_id == classroom_id)
        .order_by(QuickAction.display_order, QuickAction.quick_action_code)
    ))
    return ActionCatalog(access=access, actions=actions, quick_actions=quick_actions)


def get_action(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, action_id: uuid.UUID) -> tuple[ClassroomAccess, Action]:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    action = db.scalar(select(Action).where(Action.id == action_id, Action.classroom_id == classroom_id))
    if action is None:
        raise ActionNotFoundError("action not found")
    return access, action


def get_quick_action(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, quick_id: uuid.UUID) -> tuple[ClassroomAccess, QuickAction]:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    quick = db.scalar(select(QuickAction).where(QuickAction.id == quick_id, QuickAction.classroom_id == classroom_id))
    if quick is None:
        raise ActionNotFoundError("quick action not found")
    return access, quick


def create_action(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    code: str, name: str, kind: str, amount: int, description: str | None,
) -> Action:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    action = Action(
        id=uuid.uuid4(), classroom_id=classroom_id,
        action_code=_normalize_code(code), name=_name(name),
        action_kind=_validate_kind(kind), amount=_validate_amount(amount),
        description=_description(description), active=True,
    )
    db.add(action)
    db.flush()
    _audit(db, classroom_id=classroom_id, user_id=user_id,
           event_type="action_created", target_type="action", target_id=action.id,
           before=None, after=_action_snapshot(action))
    db.flush()
    return action


def update_action(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    action_id: uuid.UUID, code: str, name: str, kind: str,
    amount: int, description: str | None,
) -> Action:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    action = db.scalar(select(Action).where(
        Action.id == action_id, Action.classroom_id == classroom_id,
    ).with_for_update())
    if action is None:
        raise ActionNotFoundError("action not found")
    before = _action_snapshot(action)
    action.action_code = _normalize_code(code)
    action.name = _name(name)
    action.action_kind = _validate_kind(kind)
    action.amount = _validate_amount(amount)
    action.description = _description(description)
    after = _action_snapshot(action)
    if before != after:
        _audit(db, classroom_id=classroom_id, user_id=user_id,
               event_type="action_updated", target_type="action", target_id=action.id,
               before=before, after=after)
    db.flush()
    return action


def set_action_active(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    action_id: uuid.UUID, active: bool,
) -> Action:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    action = db.scalar(select(Action).where(
        Action.id == action_id, Action.classroom_id == classroom_id,
    ).with_for_update())
    if action is None:
        raise ActionNotFoundError("action not found")
    if action.active != active:
        before = _action_snapshot(action)
        action.active = active
        _audit(db, classroom_id=classroom_id, user_id=user_id,
               event_type="action_reactivated" if active else "action_deactivated",
               target_type="action", target_id=action.id,
               before=before, after=_action_snapshot(action))
    db.flush()
    return action


def _require_target_action(db: Session, *, classroom_id: uuid.UUID, action_id: uuid.UUID) -> Action:
    action = db.scalar(select(Action).where(
        Action.id == action_id, Action.classroom_id == classroom_id,
    ))
    if action is None:
        raise ActionNotFoundError("target action not found in this classroom")
    return action


def create_quick_action(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    code: str, action_id: uuid.UUID, display_order: int,
) -> QuickAction:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    _require_target_action(db, classroom_id=classroom_id, action_id=action_id)
    quick = QuickAction(
        id=uuid.uuid4(), classroom_id=classroom_id,
        quick_action_code=_normalize_code(code), action_id=action_id,
        display_order=_validate_order(display_order), active=True,
    )
    db.add(quick)
    db.flush()
    _audit(db, classroom_id=classroom_id, user_id=user_id,
           event_type="quick_action_created", target_type="quick_action", target_id=quick.id,
           before=None, after=_quick_snapshot(quick))
    db.flush()
    return quick


def update_quick_action(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    quick_id: uuid.UUID, code: str, action_id: uuid.UUID, display_order: int,
) -> QuickAction:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    quick = db.scalar(select(QuickAction).where(
        QuickAction.id == quick_id, QuickAction.classroom_id == classroom_id,
    ).with_for_update())
    if quick is None:
        raise ActionNotFoundError("quick action not found")
    _require_target_action(db, classroom_id=classroom_id, action_id=action_id)
    before = _quick_snapshot(quick)
    quick.quick_action_code = _normalize_code(code)
    quick.action_id = action_id
    quick.display_order = _validate_order(display_order)
    after = _quick_snapshot(quick)
    if before != after:
        _audit(db, classroom_id=classroom_id, user_id=user_id,
               event_type="quick_action_updated", target_type="quick_action", target_id=quick.id,
               before=before, after=after)
    db.flush()
    return quick


def set_quick_action_active(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    quick_id: uuid.UUID, active: bool,
) -> QuickAction:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    quick = db.scalar(select(QuickAction).where(
        QuickAction.id == quick_id, QuickAction.classroom_id == classroom_id,
    ).with_for_update())
    if quick is None:
        raise ActionNotFoundError("quick action not found")
    if quick.active != active:
        before = _quick_snapshot(quick)
        quick.active = active
        _audit(db, classroom_id=classroom_id, user_id=user_id,
               event_type="quick_action_reactivated" if active else "quick_action_deactivated",
               target_type="quick_action", target_id=quick.id,
               before=before, after=_quick_snapshot(quick))
    db.flush()
    return quick


def install_default_actions(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
) -> bool:
    """Create the standard ten mappings once, without overwriting custom codes.

    The caller owns the transaction. Locking the classroom serializes concurrent
    initialization attempts and avoids a partial install.
    """
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    classroom = db.scalar(select(Classroom).where(
        Classroom.id == classroom_id, Classroom.archived_at.is_(None),
    ).with_for_update())
    if classroom is None:
        raise ActionNotFoundError("classroom not found")

    action_codes = {row[0] for row in db.execute(
        select(Action.action_code).where(Action.classroom_id == classroom_id)
    )}
    quick_codes = {row[0] for row in db.execute(
        select(QuickAction.quick_action_code).where(QuickAction.classroom_id == classroom_id)
    )}
    default_action_codes = {item[0] for item in DEFAULT_ACTIONS}
    default_quick_codes = {item[1] for item in DEFAULT_ACTIONS}

    # An existing full QA sheet is left untouched, even if a teacher remapped it.
    if default_quick_codes <= quick_codes:
        return False
    if action_codes & default_action_codes or quick_codes & default_quick_codes:
        raise ActionValidationError(
            "some default A01-A10 or QA1-QA10 codes already exist; "
            "finish configuring them manually to preserve existing mappings"
        )

    actions = [
        Action(
            id=uuid.uuid4(), classroom_id=classroom_id, action_code=action_code,
            name=name, action_kind=kind, amount=amount, active=True,
        )
        for action_code, _, name, kind, amount in DEFAULT_ACTIONS
    ]
    db.add_all(actions)
    db.flush()
    db.add_all([
        QuickAction(
            id=uuid.uuid4(), classroom_id=classroom_id,
            quick_action_code=quick_code, action_id=action.id,
            display_order=index, active=True,
        )
        for index, ((_, quick_code, _, _, _), action)
        in enumerate(zip(DEFAULT_ACTIONS, actions), start=1)
    ])
    _audit(
        db, classroom_id=classroom_id, user_id=user_id,
        event_type="default_actions_installed", target_type="classroom",
        target_id=classroom_id, before=None,
        after={"action_codes": sorted(default_action_codes),
               "quick_action_codes": sorted(default_quick_codes)},
    )
    db.flush()
    return True
