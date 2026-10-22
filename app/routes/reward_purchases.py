from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import validate_csrf
from app.database import get_db
from app.services.classrooms import ClassroomNotFoundError, ClassroomPermissionError
from app.services.financial import (
    FinancialIdempotencyConflictError, RewardPurchaseNotFoundError,
    RewardPurchaseValidationError, post_reward_purchase,
)


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
