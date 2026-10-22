from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reward import Reward
from app.models.reward_purchase import RewardFulfillmentEvent, RewardPurchase
from app.models.student import Enrollment
from app.services.classrooms import ClassroomPermissionError, require_classroom_access
from app.services.financial import FinancialIntegrityError, RewardPurchaseNotFoundError


class FulfillmentValidationError(ValueError):
    pass


class FulfillmentConflictError(ValueError):
    pass


@dataclass(frozen=True)
class FulfillmentResult:
    purchase: RewardPurchase
    event: RewardFulfillmentEvent


def set_reward_fulfillment(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    purchase_id: uuid.UUID,
    target_status: str,
    expected_event_id: uuid.UUID,
    note: str | None = None,
) -> FulfillmentResult:
    """Transition an existing purchase without changing balance or inventory.

    Expected-event optimistic concurrency prevents replaying a stale form after
    a subsequent redeem/undo. Locks follow the financial refund order:
    enrollment, reward, purchase. Caller owns commit/rollback.
    """
    if any(type(v) is not uuid.UUID for v in (enrollment_id, purchase_id, expected_event_id)):
        raise FulfillmentValidationError("enrollment, purchase and event IDs must be UUIDs")
    if target_status not in {"redeemed", "unused"}:
        raise FulfillmentValidationError("unsupported fulfillment status")
    if note is not None and not isinstance(note, str):
        raise FulfillmentValidationError("note must be text")
    clean_note = note.strip() if note else ""
    if len(clean_note) > 500 or (target_status == "unused" and not clean_note):
        raise FulfillmentValidationError("a correction reason of 1–500 characters is required")

    access = require_classroom_access(
        db, user_id=actor_user_id, classroom_id=classroom_id,
    )
    if access.membership.role not in {"owner", "teacher"}:
        raise ClassroomPermissionError("owner or teacher permission is required")

    record = db.scalar(select(RewardPurchase).where(
        RewardPurchase.id == purchase_id,
        RewardPurchase.classroom_id == classroom_id,
        RewardPurchase.enrollment_id == enrollment_id,
    ))
    if record is None:
        raise RewardPurchaseNotFoundError("reward purchase not found in classroom")

    enrollment = db.scalar(select(Enrollment).where(
        Enrollment.id == enrollment_id,
        Enrollment.classroom_id == classroom_id,
    ).with_for_update().execution_options(populate_existing=True))
    if enrollment is None:
        raise FinancialIntegrityError("reward purchase enrollment is missing")
    reward = db.scalar(select(Reward).where(
        Reward.id == record.reward_id,
        Reward.classroom_id == classroom_id,
    ).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise FinancialIntegrityError("purchased reward is missing")
    purchase = db.scalar(select(RewardPurchase).where(
        RewardPurchase.id == purchase_id,
        RewardPurchase.classroom_id == classroom_id,
        RewardPurchase.enrollment_id == enrollment_id,
        RewardPurchase.reward_id == reward.id,
    ).with_for_update().execution_options(populate_existing=True))
    if purchase is None:
        raise FinancialIntegrityError("reward purchase changed while updating fulfillment")

    most_recent = db.scalar(select(RewardFulfillmentEvent).where(
        RewardFulfillmentEvent.reward_purchase_id == purchase.id,
    ).order_by(
        RewardFulfillmentEvent.occurred_at.desc(),
        RewardFulfillmentEvent.id.desc(),
    ).limit(1))
    if most_recent is None:
        raise FinancialIntegrityError("reward purchase has no fulfillment audit history")
    if most_recent.id != expected_event_id:
        raise FulfillmentConflictError("purchase status changed; refresh to review the latest state")
    if most_recent.to_status != purchase.fulfillment_status:
        raise FinancialIntegrityError("purchase status disagrees with fulfillment history")

    if purchase.fulfillment_status == "cancelled":
        raise FulfillmentConflictError("cancelled purchases cannot be redeemed or reopened")
    if purchase.fulfillment_status == target_status:
        raise FulfillmentConflictError("purchase already has that fulfillment status")
    if (purchase.fulfillment_status, target_status) not in {
        ("unused", "redeemed"), ("redeemed", "unused"),
    }:
        raise FulfillmentConflictError("invalid fulfillment transition")

    event = RewardFulfillmentEvent(
        id=uuid.uuid4(), reward_purchase_id=purchase.id,
        from_status=purchase.fulfillment_status, to_status=target_status,
        actor_user_id=actor_user_id, note=clean_note or None,
    )
    purchase.fulfillment_status = target_status
    db.add(event)
    db.flush()
    return FulfillmentResult(purchase=purchase, event=event)
