from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.config import settings
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.services.classrooms import ClassroomNotFoundError, require_classroom_access
from app.services.scanner import ScanValidationError, resolve_scan
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/scan", tags=["scanner"])


def _context(request: Request, db: Session, classroom_id: uuid.UUID):
    user = current_user_from_request(request, db)
    if user is None:
        return None, None
    try:
        access = require_classroom_access(
            db, user_id=user.id, classroom_id=classroom_id,
        )
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    if request.cookies.get(settings.active_classroom_cookie_name) != str(classroom_id):
        raise HTTPException(
            status_code=409,
            detail="select this classroom as active before using Scan Mode",
        )
    return user, access


def _page(request: Request, *, classroom, selected=None, result=None, error=None, status_code=200):
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request, name="scan_mode.html",
        context={
            "csrf_token": token, "classroom": classroom,
            "selected": selected, "result": result, "error": error,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token)
    return response


@router.get("")
def scan_page(
    classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db),
):
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    return _page(request, classroom=access.classroom)


@router.post("/resolve")
def scan_preview(
    classroom_id: uuid.UUID, request: Request,
    payload: str = Form(...), selected_enrollment_id: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        selected_id = uuid.UUID(selected_enrollment_id) if selected_enrollment_id else None
        _, result = resolve_scan(
            db, user_id=user.id, classroom_id=classroom_id,
            payload=payload, selected_enrollment_id=selected_id,
        )
    except (ScanValidationError, ValueError) as exc:
        # No guessed or forged student identifier is carried forward on failure.
        return _page(request, classroom=access.classroom, error=str(exc),
                     status_code=400)
    return _page(request, classroom=access.classroom,
                 selected=result.enrollment, result=result)
