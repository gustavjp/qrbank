from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.reward import Reward
from app.services.classrooms import (
    ClassroomAccess, ClassroomPermissionError, require_classroom_access,
)


MANAGER_ROLES = frozenset({"owner", "teacher"})
REWARD_TYPES = frozenset({"physical", "privilege", "other"})
REWARD_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,32}$")
MAX_PRICE = 2**31 - 1


class RewardValidationError(ValueError):
    pass


class RewardNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class RewardCatalog:
    access: ClassroomAccess
    rewards: list[Reward]


def _code(value: str) -> str:
    code = value.strip().upper()
    if not REWARD_CODE_PATTERN.fullmatch(code):
        raise RewardValidationError(
            "reward code must contain 1-32 letters, numbers, underscores, or hyphens"
        )
    return code


def _name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 120:
        raise RewardValidationError("reward name must contain 1-120 characters")
    return name


def _description(value: str | None) -> str | None:
    description = value.strip() if value else ""
    if len(description) > 2000:
        raise RewardValidationError("description must contain at most 2000 characters")
    return description or None


def _type(value: str) -> str:
    if value not in REWARD_TYPES:
        raise RewardValidationError("choose a valid reward type")
    return value


def _price(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PRICE:
        raise RewardValidationError("reward price must be a positive whole number")
    return value


def _manager(db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID) -> ClassroomAccess:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    if access.membership.role not in MANAGER_ROLES:
        raise ClassroomPermissionError("owner or teacher permission is required")
    return access


def _snapshot(reward: Reward) -> dict:
    return {
        "code": reward.reward_code,
        "name": reward.name,
        "description": reward.description,
        "reward_type": reward.reward_type,
        "price_points": reward.price_points,
        "active": reward.active,
        "inventory_tracking": reward.inventory_tracking,
        "quantity_available": reward.quantity_available,
    }


def _audit(
    db: Session, *, reward: Reward, user_id: uuid.UUID,
    event_type: str, before: dict | None, after: dict,
) -> None:
    db.add(AdminAuditEvent(
        id=uuid.uuid4(), classroom_id=reward.classroom_id,
        actor_user_id=user_id, event_type=event_type,
        target_type="reward", target_id=reward.id,
        before_json=before, after_json=after,
    ))


def list_rewards(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
) -> RewardCatalog:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    rewards = list(db.scalars(
        select(Reward).where(
            Reward.classroom_id == classroom_id,
        ).order_by(Reward.reward_code)
    ))
    return RewardCatalog(access=access, rewards=rewards)


def get_reward(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, reward_id: uuid.UUID,
) -> tuple[ClassroomAccess, Reward]:
    access = require_classroom_access(db, user_id=user_id, classroom_id=classroom_id)
    reward = db.scalar(select(Reward).where(
        Reward.classroom_id == classroom_id, Reward.id == reward_id,
    ))
    if reward is None:
        raise RewardNotFoundError("reward not found")
    return access, reward


def create_reward(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    code: str, name: str, description: str | None,
    reward_type: str, price_points: int,
) -> Reward:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    reward = Reward(
        id=uuid.uuid4(), classroom_id=classroom_id,
        reward_code=_code(code), name=_name(name),
        description=_description(description), reward_type=_type(reward_type),
        price_points=_price(price_points), active=True,
        inventory_tracking=False, quantity_available=None,
    )
    db.add(reward)
    db.flush()
    _audit(db, reward=reward, user_id=user_id,
           event_type="reward_created", before=None, after=_snapshot(reward))
    db.flush()
    return reward


def update_reward(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID, reward_id: uuid.UUID,
    code: str, name: str, description: str | None,
    reward_type: str, price_points: int,
) -> Reward:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    reward = db.scalar(select(Reward).where(
        Reward.classroom_id == classroom_id,
        Reward.id == reward_id,
    ).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise RewardNotFoundError("reward not found")
    # Validate all values before touching the tracked reward row.
    new_code, new_name = _code(code), _name(name)
    new_description, new_type = _description(description), _type(reward_type)
    new_price = _price(price_points)
    before = _snapshot(reward)
    reward.reward_code = new_code
    reward.name = new_name
    reward.description = new_description
    reward.reward_type = new_type
    reward.price_points = new_price
    after = _snapshot(reward)
    if before != after:
        reward.updated_at = datetime.now(timezone.utc)
        _audit(db, reward=reward, user_id=user_id,
               event_type="reward_updated", before=before, after=after)
    db.flush()
    return reward


def set_reward_active(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    reward_id: uuid.UUID, active: bool,
) -> Reward:
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    reward = db.scalar(select(Reward).where(
        Reward.classroom_id == classroom_id,
        Reward.id == reward_id,
    ).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise RewardNotFoundError("reward not found")
    if type(active) is not bool:
        raise RewardValidationError("active must be true or false")
    if reward.active != active:
        before = _snapshot(reward)
        reward.active = active
        reward.updated_at = datetime.now(timezone.utc)
        _audit(db, reward=reward, user_id=user_id,
               event_type="reward_reactivated" if active else "reward_deactivated",
               before=before, after=_snapshot(reward))
    db.flush()
    return reward
