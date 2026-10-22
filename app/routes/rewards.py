from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.services.classrooms import ClassroomNotFoundError, ClassroomPermissionError
from app.services.rewards import (
    MANAGER_ROLES, RewardNotFoundError, RewardValidationError,
    create_reward, get_reward, list_rewards, set_reward_active, update_reward,
)
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/rewards", tags=["rewards"])


def _user(request: Request, db: Session):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in required",
        )
    return user


def _page(request: Request, name: str, context: dict):
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request, name=name, context={"csrf_token": token, **context},
    )
    set_csrf_cookie(response, token)
    return response


def _submit(
    request: Request, db: Session, csrf_token: str,
    operation: Callable[[], object], destination: str,
):
    validate_csrf(request, csrf_token)
    try:
        operation()
        db.commit()
    except (ClassroomNotFoundError, RewardNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="classroom reward not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except RewardValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="reward code is already in use")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("")
def reward_catalog(
    classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        catalog = list_rewards(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    return _page(request, "reward_catalog.html", {
        "classroom": catalog.access.classroom,
        "rewards": catalog.rewards,
        "can_manage": catalog.access.membership.role in MANAGER_ROLES,
    })


@router.post("/new")
def new_reward(
    classroom_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...),
    description: str | None = Form(None), reward_type: str = Form(...),
    price_points: int = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: create_reward(
            db, user_id=user.id, classroom_id=classroom_id,
            code=code, name=name, description=description,
            reward_type=reward_type, price_points=price_points,
        ),
        f"/classrooms/{classroom_id}/rewards",
    )


@router.get("/{reward_id}/edit")
def edit_reward_page(
    classroom_id: uuid.UUID, reward_id: uuid.UUID,
    request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        access, reward = get_reward(
            db, user_id=user.id, classroom_id=classroom_id,
            reward_id=reward_id,
        )
    except (ClassroomNotFoundError, RewardNotFoundError):
        raise HTTPException(status_code=404, detail="classroom reward not found")
    if access.membership.role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    return _page(request, "reward_edit.html", {
        "classroom": access.classroom, "reward": reward,
    })


@router.post("/{reward_id}/edit")
def edit_reward(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...),
    description: str | None = Form(None), reward_type: str = Form(...),
    price_points: int = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: update_reward(
            db, user_id=user.id, classroom_id=classroom_id, reward_id=reward_id,
            code=code, name=name, description=description,
            reward_type=reward_type, price_points=price_points,
        ),
        f"/classrooms/{classroom_id}/rewards",
    )


@router.post("/{reward_id}/active")
def change_reward_status(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    active: bool = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: set_reward_active(
            db, user_id=user.id, classroom_id=classroom_id,
            reward_id=reward_id, active=active,
        ),
        f"/classrooms/{classroom_id}/rewards",
    )
