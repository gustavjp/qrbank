from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.services.classrooms import (
    ClassroomNotFoundError,
    ClassroomPermissionError,
)
from app.services.financial import NegativeBalanceNotAllowedError
from app.services.students import (
    STUDENT_MANAGER_ROLES,
    StudentNotFoundError,
    StudentValidationError,
    create_student_enrollment,
    get_enrollment,
    list_enrollment_transactions,
    list_enrollments,
    set_enrollment_active,
    update_student_enrollment,
)
from app.web import templates


router = APIRouter(
    prefix="/classrooms/{classroom_id}/students",
    tags=["students"],
)


def _form_response(
    request: Request,
    *,
    template_name: str,
    context: dict | None = None,
    status_code: int = 200,
):
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"csrf_token": csrf_token, **(context or {})},
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="student enrollment not found",
    )


@router.get("")
def student_list(
    classroom_id: uuid.UUID,
    request: Request,
    show: str = "all",
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        access, rows = list_enrollments(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            include_inactive=show != "active",
        )
    except ClassroomNotFoundError:
        raise _not_found()

    return _form_response(
        request,
        template_name="student_list.html",
        context={
            "classroom": access.classroom,
            "access": access,
            "rows": rows,
            "show": show,
            "can_manage": access.membership.role in STUDENT_MANAGER_ROLES,
        },
    )


@router.get("/new")
def new_student_page(
    classroom_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        access, _ = list_enrollments(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise _not_found()

    if access.membership.role not in STUDENT_MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner or teacher permission is required",
        )

    return _form_response(
        request,
        template_name="student_new.html",
        context={"classroom": access.classroom},
    )


@router.post("/new")
def create_student(
    classroom_id: uuid.UUID,
    request: Request,
    student_code: str = Form(...),
    display_name: str | None = Form(None),
    notes: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        record = create_student_enrollment(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            student_code=student_code,
            display_name=display_name,
            notes=notes,
        )
        db.commit()
    except ClassroomNotFoundError:
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner or teacher permission is required",
        )
    except (StudentValidationError, NegativeBalanceNotAllowedError) as exc:
        db.rollback()
        return _form_response(
            request,
            template_name="student_new.html",
            context={
                "classroom": None,
                "error": str(exc),
                "student_code": student_code,
                "display_name": display_name,
                "notes": notes,
                "classroom_id": classroom_id,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except IntegrityError:
        db.rollback()
        return _form_response(
            request,
            template_name="student_new.html",
            context={
                "classroom": None,
                "error": "That student code is already in use in this classroom.",
                "student_code": student_code,
                "display_name": display_name,
                "notes": notes,
                "classroom_id": classroom_id,
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    return RedirectResponse(
        f"/classrooms/{classroom_id}/students/{record.enrollment.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{enrollment_id}")
def student_detail(
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    request: Request,
    saved: int = 0,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        record = get_enrollment(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
        )
        transactions = list_enrollment_transactions(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
        )
    except (ClassroomNotFoundError, StudentNotFoundError):
        raise _not_found()

    return _form_response(
        request,
        template_name="student_detail.html",
        context={
            "record": record,
            "classroom": record.access.classroom,
            "student": record.student,
            "enrollment": record.enrollment,
            "transactions": transactions,
            "can_manage": record.access.membership.role in STUDENT_MANAGER_ROLES,
            "saved": bool(saved),
        },
    )


@router.post("/{enrollment_id}/edit")
def edit_student(
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    request: Request,
    student_code: str = Form(...),
    display_name: str | None = Form(None),
    notes: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        update_student_enrollment(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            student_code=student_code,
            display_name=display_name,
            notes=notes,
        )
        db.commit()
    except (ClassroomNotFoundError, StudentNotFoundError):
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner or teacher permission is required",
        )
    except StudentValidationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that student code is already in use in this classroom",
        )

    return RedirectResponse(
        f"/classrooms/{classroom_id}/students/{enrollment_id}?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{enrollment_id}/deactivate")
def deactivate_student(
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    return _set_status(
        classroom_id=classroom_id,
        enrollment_id=enrollment_id,
        active=False,
        request=request,
        csrf_token=csrf_token,
        db=db,
    )


@router.post("/{enrollment_id}/reactivate")
def reactivate_student(
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    return _set_status(
        classroom_id=classroom_id,
        enrollment_id=enrollment_id,
        active=True,
        request=request,
        csrf_token=csrf_token,
        db=db,
    )


def _set_status(
    *,
    classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    active: bool,
    request: Request,
    csrf_token: str,
    db: Session,
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        set_enrollment_active(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            active=active,
        )
        db.commit()
    except (ClassroomNotFoundError, StudentNotFoundError):
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner or teacher permission is required",
        )

    return RedirectResponse(
        f"/classrooms/{classroom_id}/students/{enrollment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
