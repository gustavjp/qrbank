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
from app.services.job_assignments import (
    JobAssignmentNotFoundError,
    JobAssignmentValidationError,
    create_job_assignment,
    end_job_assignment,
    list_job_assignments,
)
from app.services.jobs import MANAGER_ROLES
from app.web import templates


router = APIRouter(
    prefix="/classrooms/{classroom_id}/job-assignments", tags=["job-assignments"],
)


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
    except (ClassroomNotFoundError, JobAssignmentNotFoundError):
        db.rollback()
        raise HTTPException(status_code=404, detail="classroom assignment not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="owner or teacher permission is required")
    except JobAssignmentValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="this student already has an active assignment to that job")
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("")
def assignment_catalog(
    classroom_id: uuid.UUID, request: Request,
    enrollment_id: uuid.UUID | None = None, db: Session = Depends(get_db),
):
    user = _user(request, db)
    try:
        catalog = list_job_assignments(
            db, user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id,
        )
    except (ClassroomNotFoundError, JobAssignmentNotFoundError):
        raise HTTPException(status_code=404, detail="classroom assignment not found")
    return _page(request, "job_assignments.html", {
        "classroom": catalog.access.classroom,
        "rows": catalog.assignments,
        "active_enrollments": catalog.active_enrollments,
        "active_jobs": catalog.active_jobs,
        "selected_enrollment_id": catalog.selected_enrollment_id,
        "can_manage": catalog.access.membership.role in MANAGER_ROLES,
    })


@router.post("/new")
def new_assignment(
    classroom_id: uuid.UUID, request: Request,
    enrollment_id: uuid.UUID = Form(...), job_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: create_job_assignment(
            db, user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, job_id=job_id,
        ),
        f"/classrooms/{classroom_id}/job-assignments?enrollment_id={enrollment_id}",
    )


@router.post("/{assignment_id}/end")
def end_assignment(
    classroom_id: uuid.UUID, assignment_id: uuid.UUID,
    request: Request, csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    return _submit(
        request, db, csrf_token,
        lambda: end_job_assignment(
            db, user_id=user.id, classroom_id=classroom_id,
            assignment_id=assignment_id,
        ),
        f"/classrooms/{classroom_id}/job-assignments",
    )
