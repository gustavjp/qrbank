from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.reward import Reward
from app.models.reward_purchase import RewardInventoryEvent
from app.services.classrooms import (
    ClassroomPermissionError, require_classroom_access,
)
from app.services.rewards import RewardNotFoundError, RewardValidationError


MAX_STOCK = 2**31 - 1


class RewardInventoryValidationError(RewardValidationError):
    pass


class RewardInventoryConflictError(ValueError):
    pass


@dataclass(frozen=True)
class RewardInventoryResult:
    reward: Reward
    event: RewardInventoryEvent | None


def _validate_quantity(quantity: int) -> None:
    if type(quantity) is not int or not 0 <= quantity <= MAX_STOCK:
        raise RewardInventoryValidationError(
            "stock must be a nonnegative whole number within the supported range"
        )


def _validate_note(note: str) -> str:
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise RewardInventoryValidationError("an inventory reason of 1-500 characters is required")
    return note.strip()


def _manager_and_locked_reward(
    db: Session, *, actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID, reward_id: uuid.UUID,
) -> Reward:
    access = require_classroom_access(
        db, user_id=actor_user_id, classroom_id=classroom_id,
    )
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError("owner or teacher permission is required")
    reward = db.scalar(select(Reward).where(
        Reward.id == reward_id, Reward.classroom_id == classroom_id,
    ).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise RewardNotFoundError("reward not found in classroom")
    return reward


def _audit(
    db: Session, *, reward: Reward, actor_user_id: uuid.UUID,
    event_type: str, old_tracking: bool, old_quantity: int | None,
    note: str,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=reward.classroom_id,
        actor_user_id=actor_user_id, event_type=event_type,
        target_type="reward", target_id=reward.id,
        before_json={"inventory_tracking": old_tracking, "quantity_available": old_quantity},
        after_json={
            "inventory_tracking": reward.inventory_tracking,
            "quantity_available": reward.quantity_available,
            "reason": note,
        },
    ))


def _event(
    db: Session, *, reward: Reward, actor_user_id: uuid.UUID,
    event_type: str, before: int, after: int, note: str,
) -> RewardInventoryEvent:
    record = RewardInventoryEvent(
        id=uuid.uuid4(), classroom_id=reward.classroom_id,
        reward_id=reward.id, actor_user_id=actor_user_id,
        event_type=event_type, delta=after - before,
        quantity_before=before, quantity_after=after, note=note,
    )
    db.add(record)
    return record


def enable_reward_inventory(
    db: Session, *, actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID, reward_id: uuid.UUID,
    initial_quantity: int, note: str,
) -> RewardInventoryResult:
    """Enable tracking once, atomically recording the initial count.

    A zero initial count has an admin audit record but no inventory event,
    because the inventory event table requires nonzero deltas.
    """
    _validate_quantity(initial_quantity)
    clean_note = _validate_note(note)
    reward = _manager_and_locked_reward(
        db, actor_user_id=actor_user_id,
        classroom_id=classroom_id, reward_id=reward_id,
    )
    if reward.inventory_tracking:
        raise RewardInventoryConflictError("inventory tracking is already enabled")
    if reward.quantity_available is not None:
        raise RewardInventoryConflictError("untracked reward has an unexpected quantity")

    reward.inventory_tracking = True
    reward.quantity_available = initial_quantity
    reward.updated_at = datetime.now(timezone.utc)
    event = _event(
        db, reward=reward, actor_user_id=actor_user_id,
        event_type="initial_stock", before=0, after=initial_quantity,
        note=clean_note,
    ) if initial_quantity else None
    _audit(
        db, reward=reward, actor_user_id=actor_user_id,
        event_type="reward_inventory_enabled",
        old_tracking=False, old_quantity=None,
        note=clean_note,
    )
    db.flush()
    return RewardInventoryResult(reward=reward, event=event)


def adjust_reward_inventory(
    db: Session, *, actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID, reward_id: uuid.UUID,
    expected_quantity: int, new_quantity: int, note: str,
) -> RewardInventoryResult:
    """Set an exact stock count with a guarded, audited delta.

    Locking the same reward row as purchases and refunds prevents lost stock
    updates. The expected count rejects stale inventory forms.
    """
    _validate_quantity(expected_quantity)
    _validate_quantity(new_quantity)
    clean_note = _validate_note(note)
    reward = _manager_and_locked_reward(
        db, actor_user_id=actor_user_id,
        classroom_id=classroom_id, reward_id=reward_id,
    )
    if not reward.inventory_tracking or reward.quantity_available is None:
        raise RewardInventoryConflictError("enable inventory tracking first")
    if reward.quantity_available != expected_quantity:
        raise RewardInventoryConflictError("stock changed; refresh and review the latest count")
    if new_quantity == expected_quantity:
        raise RewardInventoryConflictError("stock is already at this count")

    reward.quantity_available = new_quantity
    reward.updated_at = datetime.now(timezone.utc)
    event = _event(
        db, reward=reward, actor_user_id=actor_user_id,
        event_type="manual_adjustment", before=expected_quantity,
        after=new_quantity, note=clean_note,
    )
    _audit(
        db, reward=reward, actor_user_id=actor_user_id,
        event_type="reward_inventory_adjusted",
        old_tracking=True, old_quantity=expected_quantity,
        note=clean_note,
    )
    db.flush()
    return RewardInventoryResult(reward=reward, event=event)
