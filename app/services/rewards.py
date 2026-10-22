from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AdminAuditEvent
from app.models.reward import Reward
from app.models.classroom import ClassroomSettings
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


class RewardAvailabilityConflictError(ValueError):
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
        "available_from": reward.available_from.isoformat() if reward.available_from else None,
        "available_until": reward.available_until.isoformat() if reward.available_until else None,
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


def reward_availability_status(reward: Reward, now: datetime) -> str:
    """Return the status enforced by financial purchase posting for active rewards."""
    if not reward.active:
        return "inactive"
    if reward.available_from is not None and now < reward.available_from:
        return "not_yet_available"
    if reward.available_until is not None and now >= reward.available_until:
        return "no_longer_available"
    return "available"


def classroom_local_time(value: datetime | None, timezone_name: str) -> str:
    """Render a persisted instant for an HTML datetime-local input."""
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M") if value else ""


def _local_datetime_to_utc(value: str, zone: ZoneInfo) -> datetime | None:
    clean = value.strip()
    if not clean:
        return None
    try:
        naive = datetime.strptime(clean, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise RewardValidationError("use a valid local date and time") from exc
    valid_utc_instants = {
        candidate.astimezone(timezone.utc)
        for fold in (0, 1)
        if (candidate := naive.replace(tzinfo=zone, fold=fold)).astimezone(
            timezone.utc
        ).astimezone(zone).replace(tzinfo=None) == naive
    }
    if not valid_utc_instants:
        raise RewardValidationError("that local time does not exist because of a clock change")
    if len(valid_utc_instants) != 1:
        raise RewardValidationError("that local time is ambiguous because of a clock change")
    return valid_utc_instants.pop()


def set_reward_availability(
    db: Session, *, user_id: uuid.UUID, classroom_id: uuid.UUID,
    reward_id: uuid.UUID, available_from_local: str,
    available_until_local: str, expected_from: str,
    expected_until: str, note: str | None = None,
) -> Reward:
    """Configure a reward's optional inclusive-start/exclusive-end purchase window.

    Input datetimes are classroom-local. Old-window form tokens prevent an
    outdated edit from overwriting another teacher's change.
    """
    _manager(db, user_id=user_id, classroom_id=classroom_id)
    settings = db.get(ClassroomSettings, classroom_id)
    if settings is None:
        raise RewardValidationError("classroom timezone settings are missing")
    try:
        zone = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError as exc:
        raise RewardValidationError("classroom timezone is invalid") from exc
    if any(type(v) is not str for v in (
        available_from_local, available_until_local, expected_from, expected_until,
    )):
        raise RewardValidationError("availability fields must be text")
    if note is not None and not isinstance(note, str):
        raise RewardValidationError("availability note must be text")
    clean_note = note.strip() if note else ""
    if len(clean_note) > 500:
        raise RewardValidationError("availability note must be at most 500 characters")
    start = _local_datetime_to_utc(available_from_local, zone)
    end = _local_datetime_to_utc(available_until_local, zone)
    if start is not None and end is not None and start >= end:
        raise RewardValidationError("the availability end must be after the start")

    reward = db.scalar(select(Reward).where(
        Reward.id == reward_id, Reward.classroom_id == classroom_id,
    ).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise RewardNotFoundError("reward not found")
    if (
        (reward.available_from.isoformat() if reward.available_from else "") != expected_from
        or (reward.available_until.isoformat() if reward.available_until else "") != expected_until
    ):
        raise RewardAvailabilityConflictError(
            "availability changed; refresh the page to review the current schedule"
        )

    if reward.available_from != start or reward.available_until != end:
        before = _snapshot(reward)
        reward.available_from = start
        reward.available_until = end
        reward.updated_at = datetime.now(timezone.utc)
        after = _snapshot(reward)
        if clean_note:
            after["reason"] = clean_note
        _audit(
            db, reward=reward, user_id=user_id,
            event_type="reward_availability_updated", before=before, after=after,
        )
    db.flush()
    return reward

