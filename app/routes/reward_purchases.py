from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.models.auth import User
from app.models.reward_purchase import RewardFulfillmentEvent, RewardPurchase
from app.models.student import Enrollment
from app.services.classrooms import (
    ClassroomNotFoundError, ClassroomPermissionError, require_classroom_access,
)
from app.services.financial import (
    FinancialCorrectionValidationError, FinancialIdempotencyConflictError,
    RewardPurchaseNotFoundError, RewardPurchaseValidationError,
    post_reward_purchase, post_reward_refund,
)
from app.services.reward_fulfillment import (
    FulfillmentConflictError, FulfillmentValidationError, set_reward_fulfillment,
)
from app.web import templates


router = APIRouter(tags=["reward_purchases"])


REJECTION_MESSAGES = {
    "inactive_student": "This student is inactive and cannot make purchases.",
    "inactive_reward": "This reward is inactive.",
    "not_yet_available": "This reward is not available yet.",
    "no_longer_available": "This reward is no longer available.",
    "price_changed": "The reward price changed. Refresh the page to review the new price.",
    "out_of_stock": "This reward is out of stock.",
    "insufficient_balance": "The student does not have enough points for this reward.",
}


def _purchase(
    db: Session,
    *,
    request: Request,
    csrf_token: str,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    reward_id: uuid.UUID,
    expected_price: int,
    request_id: uuid.UUID,
    redirect_path: str,
):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in required")
    validate_csrf(request, csrf_token)
    try:
        result = post_reward_purchase(
            db, actor_user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, reward_id=reward_id,
            expected_price=expected_price, request_id=request_id,
        )
        db.commit()
    except (ClassroomNotFoundError, RewardPurchaseNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="student or reward not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except FinancialIdempotencyConflictError:
        db.rollback()
        raise HTTPException(status_code=409, detail="request ID was reused with different inputs")
    except RewardPurchaseValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    if result.rejection_code is not None:
        raise HTTPException(
            status_code=422,
            detail=REJECTION_MESSAGES.get(result.rejection_code, "Purchase rejected; no points changed."),
        )
    return RedirectResponse(
        redirect_path + ("?replayed=1" if result.replayed else "?purchased=1"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/classrooms/{classroom_id}/students/{enrollment_id}/rewards/purchase")
def purchase_from_student(
    classroom_id: uuid.UUID, enrollment_id: uuid.UUID, request: Request,
    reward_id: uuid.UUID = Form(...), expected_price: int = Form(...),
    request_id: uuid.UUID = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    return _purchase(
        db, request=request, csrf_token=csrf_token,
        classroom_id=classroom_id, enrollment_id=enrollment_id,
        reward_id=reward_id, expected_price=expected_price, request_id=request_id,
        redirect_path=f"/classrooms/{classroom_id}/students/{enrollment_id}",
    )


@router.post("/classrooms/{classroom_id}/rewards/{reward_id}/purchase")
def purchase_from_catalog(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    enrollment_id: uuid.UUID = Form(...), expected_price: int = Form(...),
    request_id: uuid.UUID = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    return _purchase(
        db, request=request, csrf_token=csrf_token,
        classroom_id=classroom_id, enrollment_id=enrollment_id,
        reward_id=reward_id, expected_price=expected_price, request_id=request_id,
        redirect_path=f"/classrooms/{classroom_id}/rewards",
    )


REFUND_REJECTION_MESSAGES = {
    "already_refunded": "This reward purchase was already cancelled or refunded.",
    "already_redeemed": "A redeemed reward cannot be refunded through this form.",
    "balance_out_of_range": "The refund would exceed the supported account balance.",
    "stock_limit_exceeded": "Restoring the reward would exceed the inventory limit.",
}


@router.post(
    "/classrooms/{classroom_id}/students/{enrollment_id}/"
    "reward-purchases/{purchase_id}/refund"
)
def refund_reward_purchase(
    classroom_id: uuid.UUID, enrollment_id: uuid.UUID, purchase_id: uuid.UUID,
    request: Request, note: str = Form(...),
    request_id: uuid.UUID = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    validate_csrf(request, csrf_token)
    try:
        result = post_reward_refund(
            db, actor_user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, purchase_id=purchase_id,
            note=note, request_id=request_id,
        )
        db.commit()
    except (ClassroomNotFoundError, RewardPurchaseNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="reward purchase not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except FinancialIdempotencyConflictError:
        db.rollback()
        raise HTTPException(status_code=409, detail="request ID was reused with different inputs")
    except (FinancialCorrectionValidationError, RewardPurchaseValidationError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    if result.rejection_code:
        raise HTTPException(
            status_code=422,
            detail=REFUND_REJECTION_MESSAGES.get(
                result.rejection_code, "Reward refund rejected; no points changed."
            ),
        )
    return RedirectResponse(
        f"/classrooms/{classroom_id}/students/{enrollment_id}"
        + ("?replayed=1" if result.replayed else "?refunded=1"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/classrooms/{classroom_id}/reward-purchases")
def reward_purchase_history(
    classroom_id: uuid.UUID, request: Request, show: str = "all",
    changed: int = 0, db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if show not in {"all", "unused", "redeemed", "cancelled"}:
        raise HTTPException(status_code=400, detail="invalid purchase status filter")
    try:
        access = require_classroom_access(
            db, user_id=user.id, classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    statement = select(RewardPurchase, Enrollment.student_code).join(
        Enrollment, Enrollment.id == RewardPurchase.enrollment_id,
    ).where(
        RewardPurchase.classroom_id == classroom_id,
        Enrollment.classroom_id == classroom_id,
    )
    if show != "all":
        statement = statement.where(RewardPurchase.fulfillment_status == show)
    records = db.execute(statement.order_by(
        RewardPurchase.purchased_at.desc(), RewardPurchase.id.desc(),
    )).all()

    purchase_ids = [record.id for record, _ in records]
    events = list(db.scalars(select(RewardFulfillmentEvent).where(
        RewardFulfillmentEvent.reward_purchase_id.in_(purchase_ids),
    ).order_by(
        RewardFulfillmentEvent.occurred_at.desc(),
        RewardFulfillmentEvent.id.desc(),
    ))) if purchase_ids else []
    events_by_purchase = {purchase_id: [] for purchase_id in purchase_ids}
    latest_event_ids = {}
    for event in events:
        events_by_purchase[event.reward_purchase_id].append(event)
        latest_event_ids.setdefault(event.reward_purchase_id, event.id)
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id is not None}
    actors = {u.id: u.email for u in db.scalars(select(User).where(
        User.id.in_(actor_ids),
    ))} if actor_ids else {}

    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request, name="reward_purchase_history.html",
        context={
            "classroom": access.classroom,
            "records": records,
            "events_by_purchase": events_by_purchase,
            "latest_event_ids": latest_event_ids,
            "actors": actors,
            "show": show, "changed": bool(changed),
            "can_manage": access.membership.role in {"owner", "teacher"},
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _submit_fulfillment(
    db: Session, request: Request, *, csrf_token: str,
    classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
    purchase_id: uuid.UUID, target_status: str,
    expected_event_id: uuid.UUID, note: str | None,
    destination: str,
):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    validate_csrf(request, csrf_token)
    try:
        set_reward_fulfillment(
            db, actor_user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, purchase_id=purchase_id,
            target_status=target_status, expected_event_id=expected_event_id,
            note=note,
        )
        db.commit()
    except (ClassroomNotFoundError, RewardPurchaseNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="reward purchase not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except FulfillmentValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except FulfillmentConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/classrooms/{classroom_id}/reward-purchases/{purchase_id}/fulfill")
def fulfill_from_history(
    classroom_id: uuid.UUID, purchase_id: uuid.UUID, request: Request,
    enrollment_id: uuid.UUID = Form(...), target_status: str = Form(...),
    expected_event_id: uuid.UUID = Form(...), note: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    return _submit_fulfillment(
        db, request, csrf_token=csrf_token,
        classroom_id=classroom_id, enrollment_id=enrollment_id,
        purchase_id=purchase_id, target_status=target_status,
        expected_event_id=expected_event_id, note=note,
        destination=f"/classrooms/{classroom_id}/reward-purchases?changed=1",
    )


@router.post(
    "/classrooms/{classroom_id}/students/{enrollment_id}/"
    "reward-purchases/{purchase_id}/fulfill"
)
def fulfill_from_student(
    classroom_id: uuid.UUID, enrollment_id: uuid.UUID, purchase_id: uuid.UUID,
    request: Request, target_status: str = Form(...),
    expected_event_id: uuid.UUID = Form(...), note: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    return _submit_fulfillment(
        db, request, csrf_token=csrf_token,
        classroom_id=classroom_id, enrollment_id=enrollment_id,
        purchase_id=purchase_id, target_status=target_status,
        expected_event_id=expected_event_id, note=note,
        destination=f"/classrooms/{classroom_id}/students/{enrollment_id}?fulfilled=1",
    )
