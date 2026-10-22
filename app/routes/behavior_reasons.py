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
from app.services.behavior_reasons import (
    MANAGER_ROLES, REASON_TYPES,
    BehaviorReasonNotFoundError, BehaviorReasonValidationError,
    create_reason, get_event_detail, list_reasons, set_reason_active,
    update_event_detail, update_reason,
)
from app.services.classrooms import ClassroomNotFoundError, ClassroomPermissionError
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/behavior", tags=["behavior"])


def _user(request: Request, db: Session):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


def _page(request: Request, name: str, context: dict):
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request, name=name,
        context={"csrf_token": token, **context},
    )
    set_csrf_cookie(response, token)
    return response


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="behavior item not found")


def _submit(
    request: Request, db: Session, csrf_token: str,
    operation: Callable[[], object], destination: str,
):
    validate_csrf(request, csrf_token)
    try:
        operation()
        db.commit()
    except (ClassroomNotFoundError, BehaviorReasonNotFoundError):
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except BehaviorReasonValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="behavior reason code is already in use")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/reasons")
def reason_catalog(
    classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        catalog = list_reasons(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError:
        raise _not_found()
    return _page(request, "behavior_reasons.html", {
        "classroom": catalog.access.classroom,
        "reasons": catalog.reasons,
        "types": REASON_TYPES,
        "can_manage": catalog.access.membership.role in MANAGER_ROLES,
    })


@router.post("/reasons/new")
def new_reason(
    classroom_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...),
    behavior_type: str = Form(...), description: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: create_reason(
            db, user_id=user.id, classroom_id=classroom_id,
            code=code, name=name, behavior_type=behavior_type,
            description=description,
        ),
        f"/classrooms/{classroom_id}/behavior/reasons",
    )


@router.post("/reasons/{reason_id}/edit")
def edit_reason(
    classroom_id: uuid.UUID, reason_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...),
    description: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: update_reason(
            db, user_id=user.id, classroom_id=classroom_id,
            reason_id=reason_id, code=code, name=name, description=description,
        ),
        f"/classrooms/{classroom_id}/behavior/reasons",
    )


@router.post("/reasons/{reason_id}/active")
def change_reason_status(
    classroom_id: uuid.UUID, reason_id: uuid.UUID, request: Request,
    active: bool = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: set_reason_active(
            db, user_id=user.id, classroom_id=classroom_id,
            reason_id=reason_id, active=active,
        ),
        f"/classrooms/{classroom_id}/behavior/reasons",
    )


@router.get("/events/{event_id}/edit")
def event_detail_page(
    classroom_id: uuid.UUID, event_id: uuid.UUID, request: Request,
    saved: int = 0, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        detail = get_event_detail(
            db, user_id=user.id, classroom_id=classroom_id, event_id=event_id,
        )
    except (ClassroomNotFoundError, BehaviorReasonNotFoundError):
        raise _not_found()
    return _page(request, "behavior_detail.html", {
        "classroom": detail.access.classroom,
        "event": detail.event,
        "choices": detail.choices,
        "audits": detail.audits,
        "saved": bool(saved),
        "can_manage": detail.access.membership.role in MANAGER_ROLES,
    })


@router.post("/events/{event_id}/edit")
def save_event_detail(
    classroom_id: uuid.UUID, event_id: uuid.UUID, request: Request,
    reason_id: str = Form(""), note: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    validate_csrf(request, csrf_token)
    try:
        parsed_reason_id = uuid.UUID(reason_id) if reason_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid behavior reason ID") from exc
    return _submit(
        request, db, csrf_token,
        lambda: update_event_detail(
            db, user_id=user.id, classroom_id=classroom_id,
            event_id=event_id, reason_id=parsed_reason_id, note=note,
        ),
        f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit?saved=1",
    )
