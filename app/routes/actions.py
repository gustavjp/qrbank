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
from app.services.actions import (
    ACTION_KINDS,
    MANAGER_ROLES,
    ActionNotFoundError,
    ActionValidationError,
    create_action,
    create_quick_action,
    get_action,
    get_quick_action,
    install_default_actions,
    list_catalog,
    set_action_active,
    set_quick_action_active,
    update_action,
    update_quick_action,
)
from app.services.classrooms import ClassroomNotFoundError, ClassroomPermissionError
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/actions", tags=["actions"])


def _user(request: Request, db: Session):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in required")
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
    return HTTPException(status_code=404, detail="classroom action not found")


def _manager_role(role: str) -> None:
    if role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")


def _submit(
    request: Request, db: Session, csrf_token: str,
    operation: Callable[[], object], destination: str,
):
    validate_csrf(request, csrf_token)
    try:
        operation()
        db.commit()
    except (ClassroomNotFoundError, ActionNotFoundError):
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except ActionValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="action or quick-action code is already in use")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("")
def action_catalog(classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    try:
        catalog = list_catalog(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError:
        raise _not_found()
    return _page(request, "action_catalog.html", {
        "classroom": catalog.access.classroom,
        "actions": catalog.actions,
        "quick_actions": catalog.quick_actions,
        "action_by_id": {item.id: item for item in catalog.actions},
        "can_manage": catalog.access.membership.role in MANAGER_ROLES,
        "kinds": sorted(ACTION_KINDS),
    })


@router.post("/defaults")
def install_defaults(
    classroom_id: uuid.UUID, request: Request,
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: install_default_actions(db, user_id=user.id, classroom_id=classroom_id),
        f"/classrooms/{classroom_id}/actions",
    )


@router.post("/new")
def new_action(
    classroom_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...), kind: str = Form(...),
    amount: int = Form(...), description: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: create_action(db, user_id=user.id, classroom_id=classroom_id,
                              code=code, name=name, kind=kind, amount=amount,
                              description=description),
        f"/classrooms/{classroom_id}/actions")


@router.get("/{action_id}/edit")
def edit_action_page(
    classroom_id: uuid.UUID, action_id: uuid.UUID,
    request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        access, action = get_action(db, user_id=user.id, classroom_id=classroom_id, action_id=action_id)
    except (ClassroomNotFoundError, ActionNotFoundError):
        raise _not_found()
    _manager_role(access.membership.role)
    return _page(request, "action_edit.html", {
        "classroom": access.classroom, "action": action, "kinds": sorted(ACTION_KINDS),
    })


@router.post("/{action_id}/edit")
def edit_action(
    classroom_id: uuid.UUID, action_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...), kind: str = Form(...),
    amount: int = Form(...), description: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: update_action(db, user_id=user.id, classroom_id=classroom_id,
                              action_id=action_id, code=code, name=name, kind=kind,
                              amount=amount, description=description),
        f"/classrooms/{classroom_id}/actions")


@router.post("/{action_id}/active")
def change_action_status(
    classroom_id: uuid.UUID, action_id: uuid.UUID, request: Request,
    active: bool = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: set_action_active(db, user_id=user.id, classroom_id=classroom_id,
                                  action_id=action_id, active=active),
        f"/classrooms/{classroom_id}/actions")


@router.post("/quick/new")
def new_quick_action(
    classroom_id: uuid.UUID, request: Request,
    code: str = Form(...), action_id: uuid.UUID = Form(...),
    display_order: int = Form(0), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: create_quick_action(db, user_id=user.id, classroom_id=classroom_id,
                                    code=code, action_id=action_id, display_order=display_order),
        f"/classrooms/{classroom_id}/actions")


@router.get("/quick/{quick_id}/edit")
def edit_quick_page(
    classroom_id: uuid.UUID, quick_id: uuid.UUID,
    request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        access, quick = get_quick_action(db, user_id=user.id, classroom_id=classroom_id, quick_id=quick_id)
        catalog = list_catalog(db, user_id=user.id, classroom_id=classroom_id)
    except (ClassroomNotFoundError, ActionNotFoundError):
        raise _not_found()
    _manager_role(access.membership.role)
    return _page(request, "quick_action_edit.html", {
        "classroom": access.classroom, "quick": quick, "actions": catalog.actions,
    })


@router.post("/quick/{quick_id}/edit")
def edit_quick_action(
    classroom_id: uuid.UUID, quick_id: uuid.UUID, request: Request,
    code: str = Form(...), action_id: uuid.UUID = Form(...),
    display_order: int = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: update_quick_action(db, user_id=user.id, classroom_id=classroom_id,
                                    quick_id=quick_id, code=code, action_id=action_id,
                                    display_order=display_order),
        f"/classrooms/{classroom_id}/actions")


@router.post("/quick/{quick_id}/active")
def change_quick_status(
    classroom_id: uuid.UUID, quick_id: uuid.UUID, request: Request,
    active: bool = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
        lambda: set_quick_action_active(db, user_id=user.id, classroom_id=classroom_id,
                                        quick_id=quick_id, active=active),
        f"/classrooms/{classroom_id}/actions")
