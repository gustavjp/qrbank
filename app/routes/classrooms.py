from __future__ import annotations

import uuid
from datetime import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.config import settings as app_settings
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.services.classrooms import (
    ClassroomNotFoundError,
    ClassroomPermissionError,
    ClassroomValidationError,
    create_classroom_for_owner,
    get_classroom_settings,
    require_classroom_access,
    update_classroom_configuration_for_owner,
)
from app.web import templates


router = APIRouter(prefix="/classrooms", tags=["classrooms"])


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


def _parse_bool(value: str | None) -> bool:
    return value in {"1", "true", "on", "yes"}


def _parse_time(value: str, field_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ClassroomValidationError(f"{field_name} must be a valid time") from exc


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="classroom not found",
    )


@router.get("/new")
def new_classroom_page(request: Request, db: Session = Depends(get_db)):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    return _form_response(request, template_name="classroom_new.html")


@router.post("/new")
def create_classroom(
    request: Request,
    name: str = Form(...),
    school_year: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=user.id,
            name=name,
            school_year=school_year,
        )
        db.commit()
    except ClassroomValidationError as exc:
        db.rollback()
        return _form_response(
            request,
            template_name="classroom_new.html",
            context={
                "error": str(exc),
                "name": name,
                "school_year": school_year,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(
        f"/classrooms/{classroom.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=app_settings.active_classroom_cookie_name,
        value=str(classroom.id),
        httponly=True,
        secure=app_settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/{classroom_id}/select")
def select_classroom(
    classroom_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        require_classroom_access(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise _not_found()

    response = RedirectResponse(
        f"/classrooms/{classroom_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=app_settings.active_classroom_cookie_name,
        value=str(classroom_id),
        httponly=True,
        secure=app_settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/{classroom_id}")
def classroom_detail(
    classroom_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        access, classroom_settings = get_classroom_settings(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise _not_found()

    return _form_response(
        request,
        template_name="classroom_detail.html",
        context={
            "user": user,
            "access": access,
            "classroom": access.classroom,
            "settings": classroom_settings,
            "is_active": request.cookies.get(app_settings.active_classroom_cookie_name)
            == str(classroom_id),
        },
    )


@router.get("/{classroom_id}/settings")
def classroom_settings_page(
    classroom_id: uuid.UUID,
    request: Request,
    saved: int = 0,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        access, classroom_settings = get_classroom_settings(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise _not_found()

    return _form_response(
        request,
        template_name="classroom_settings.html",
        context={
            "access": access,
            "classroom": access.classroom,
            "settings": classroom_settings,
            "can_edit": access.membership.role == "owner",
            "saved": bool(saved),
        },
    )


@router.post("/{classroom_id}/settings")
def update_classroom_settings(
    classroom_id: uuid.UUID,
    request: Request,
    name: str = Form(...),
    school_year: str = Form(...),
    timezone: str = Form(...),
    starting_balance: int = Form(...),
    weekly_allowance_enabled: str | None = Form(None),
    weekly_allowance_amount: int = Form(...),
    weekly_allowance_day: int = Form(...),
    weekly_allowance_time: str = Form(...),
    weekly_wages_enabled: str | None = Form(None),
    weekly_wage_day: int = Form(...),
    weekly_wage_time: str = Form(...),
    sound_enabled: str | None = Form(None),
    duplicate_scan_seconds: int = Form(...),
    allow_negative_balance: str | None = Form(None),
    student_selection_timeout_seconds: int = Form(...),
    confirm_normal_transactions: str | None = Form(None),
    confirm_balance_reset: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        update_classroom_configuration_for_owner(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            name=name,
            school_year=school_year,
            timezone=timezone,
            starting_balance=starting_balance,
            weekly_allowance_enabled=_parse_bool(weekly_allowance_enabled),
            weekly_allowance_amount=weekly_allowance_amount,
            weekly_allowance_day=weekly_allowance_day,
            weekly_allowance_time=_parse_time(
                weekly_allowance_time,
                "weekly allowance time",
            ),
            weekly_wages_enabled=_parse_bool(weekly_wages_enabled),
            weekly_wage_day=weekly_wage_day,
            weekly_wage_time=_parse_time(
                weekly_wage_time,
                "weekly wage time",
            ),
            sound_enabled=_parse_bool(sound_enabled),
            duplicate_scan_seconds=duplicate_scan_seconds,
            allow_negative_balance=_parse_bool(allow_negative_balance),
            student_selection_timeout_seconds=student_selection_timeout_seconds,
            confirm_normal_transactions=_parse_bool(confirm_normal_transactions),
            confirm_balance_reset=_parse_bool(confirm_balance_reset),
        )
        db.commit()
    except ClassroomNotFoundError:
        db.rollback()
        raise _not_found()
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="owner permission is required",
        )
    except ClassroomValidationError as exc:
        db.rollback()
        access, classroom_settings = get_classroom_settings(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
        )
        return _form_response(
            request,
            template_name="classroom_settings.html",
            context={
                "access": access,
                "classroom": access.classroom,
                "settings": classroom_settings,
                "can_edit": access.membership.role == "owner",
                "error": str(exc),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(
        f"/classrooms/{classroom_id}/settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )
