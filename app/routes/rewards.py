from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.models.auth import User
from app.models.audit import AdminAuditEvent
from app.models.reward_purchase import RewardInventoryEvent
from app.services.classrooms import ClassroomNotFoundError, ClassroomPermissionError
from app.services.reward_inventory import (
    RewardInventoryConflictError, adjust_reward_inventory,
    enable_reward_inventory,
)
from app.services.rewards import (
    MANAGER_ROLES, RewardNotFoundError, RewardValidationError,
    create_reward, get_reward, list_rewards, set_reward_active, update_reward,
)
from app.models.student import Enrollment
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
    except RewardInventoryConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="reward code is already in use")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("")
def reward_catalog(
    classroom_id: uuid.UUID, request: Request,
    purchased: int = 0, replayed: int = 0,
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        catalog = list_rewards(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    can_manage = catalog.access.membership.role in MANAGER_ROLES
    enrollments = list(db.scalars(select(Enrollment).where(
        Enrollment.classroom_id == classroom_id,
        Enrollment.active.is_(True),
    ).order_by(Enrollment.student_code))) if can_manage else []
    return _page(request, "reward_catalog.html", {
        "classroom": catalog.access.classroom,
        "rewards": catalog.rewards,
        "can_manage": can_manage,
        "enrollments": enrollments,
        "purchase_request_ids": {r.id: uuid.uuid4() for r in catalog.rewards},
        "purchased": bool(purchased), "replayed": bool(replayed),
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


@router.get("/{reward_id}/inventory")
def reward_inventory_page(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    changed: int = 0, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        access, reward = get_reward(
            db, user_id=user.id, classroom_id=classroom_id, reward_id=reward_id,
        )
    except (ClassroomNotFoundError, RewardNotFoundError):
        raise HTTPException(status_code=404, detail="classroom reward not found")
    events = list(db.scalars(select(RewardInventoryEvent).where(
        RewardInventoryEvent.classroom_id == classroom_id,
        RewardInventoryEvent.reward_id == reward.id,
    ).order_by(
        RewardInventoryEvent.occurred_at.desc(),
        RewardInventoryEvent.id.desc(),
    )))
    setups = list(db.scalars(select(AdminAuditEvent).where(
        AdminAuditEvent.classroom_id == classroom_id,
        AdminAuditEvent.target_type == "reward",
        AdminAuditEvent.target_id == reward.id,
        AdminAuditEvent.event_type == "reward_inventory_enabled",
    ).order_by(AdminAuditEvent.occurred_at.desc(), AdminAuditEvent.id.desc())))
    actor_ids = {
        actor_id for actor_id in [
            *(event.actor_user_id for event in events),
            *(event.actor_user_id for event in setups),
        ] if actor_id is not None
    }
    actors = {u.id: u.email for u in db.scalars(select(User).where(
        User.id.in_(actor_ids),
    ))} if actor_ids else {}
    return _page(request, "reward_inventory.html", {
        "classroom": access.classroom, "reward": reward,
        "events": events, "setups": setups, "actors": actors,
        "can_manage": access.membership.role in MANAGER_ROLES,
        "changed": bool(changed),
    })


@router.post("/{reward_id}/inventory/enable")
def enable_inventory(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    initial_quantity: int = Form(...), note: str = Form(...),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: enable_reward_inventory(
            db, actor_user_id=user.id, classroom_id=classroom_id,
            reward_id=reward_id, initial_quantity=initial_quantity, note=note,
        ),
        f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory?changed=1",
    )


@router.post("/{reward_id}/inventory/adjust")
def adjust_inventory(
    classroom_id: uuid.UUID, reward_id: uuid.UUID, request: Request,
    expected_quantity: int = Form(...), new_quantity: int = Form(...),
    note: str = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: adjust_reward_inventory(
            db, actor_user_id=user.id, classroom_id=classroom_id,
            reward_id=reward_id, expected_quantity=expected_quantity,
            new_quantity=new_quantity, note=note,
        ),
        f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory?changed=1",
    )
