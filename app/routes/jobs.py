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
from app.services.jobs import (
    MANAGER_ROLES,
    JobNotFoundError,
    JobValidationError,
    create_job,
    get_job,
    list_jobs,
    set_job_active,
    update_job,
)
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/jobs", tags=["jobs"])


def _user(request: Request, db: Session):
    user = current_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in required")
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
    except (ClassroomNotFoundError, JobNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="classroom job not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except JobValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="job code is already in use")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("")
def job_catalog(classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    try:
        catalog = list_jobs(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    return _page(request, "job_catalog.html", {
        "classroom": catalog.access.classroom,
        "jobs": catalog.jobs,
        "can_manage": catalog.access.membership.role in MANAGER_ROLES,
    })


@router.post("/new")
def new_job(
    classroom_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...), weekly_wage: int = Form(...),
    notes: str | None = Form(None), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
                   lambda: create_job(db, user_id=user.id, classroom_id=classroom_id,
                                      code=code, name=name, weekly_wage=weekly_wage,
                                      notes=notes),
                   f"/classrooms/{classroom_id}/jobs")


@router.get("/{job_id}/edit")
def edit_job_page(
    classroom_id: uuid.UUID, job_id: uuid.UUID,
    request: Request, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        access, job = get_job(db, user_id=user.id, classroom_id=classroom_id, job_id=job_id)
    except (ClassroomNotFoundError, JobNotFoundError):
        raise HTTPException(status_code=404, detail="classroom job not found")
    if access.membership.role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    return _page(request, "job_edit.html", {"classroom": access.classroom, "job": job})


@router.post("/{job_id}/edit")
def edit_job(
    classroom_id: uuid.UUID, job_id: uuid.UUID, request: Request,
    code: str = Form(...), name: str = Form(...), weekly_wage: int = Form(...),
    notes: str | None = Form(None), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
                   lambda: update_job(db, user_id=user.id, classroom_id=classroom_id,
                                      job_id=job_id, code=code, name=name,
                                      weekly_wage=weekly_wage, notes=notes),
                   f"/classrooms/{classroom_id}/jobs")


@router.post("/{job_id}/active")
def change_job_status(
    classroom_id: uuid.UUID, job_id: uuid.UUID, request: Request,
    active: bool = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(request, db, csrf_token,
                   lambda: set_job_active(db, user_id=user.id, classroom_id=classroom_id,
                                          job_id=job_id, active=active),
                   f"/classrooms/{classroom_id}/jobs")
