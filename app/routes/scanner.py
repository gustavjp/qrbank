from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.config import settings
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialRequest
from app.models.student import Enrollment
from app.services.classrooms import (
    ClassroomNotFoundError, ClassroomPermissionError, require_classroom_access,
)
from app.services.financial import (
    FinancialIdempotencyConflictError, ScanPostingResult, post_scanned_action,
)
from app.services.scanner import ScanValidationError, parse_scan_command, resolve_scan
from app.services.scan_selection import (
    SelectionExpiredError, issue_selection_token, verify_selection_token,
)
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


def _page(
    request: Request, *, db: Session, classroom, selected=None, result=None,
    outcome: ScanPostingResult | None = None, confirmation=None,
    selection_valid: bool = False, error=None, status_code=200,
):
    token = csrf_token_for_request(request)
    settings_row = db.get(ClassroomSettings, classroom.id)
    if settings_row is None:
        raise HTTPException(status_code=404, detail="classroom settings not found")
    selected_token = None
    selection_expires_at = None
    if selected is not None and selection_valid:
        selected_token = issue_selection_token(
            session_token=request.cookies.get(settings.session_cookie_name, ""),
            classroom_id=classroom.id, enrollment_id=selected.id,
        )
        issued_at = int(selected_token.split(".", 1)[0])
        selection_expires_at = issued_at + settings_row.student_selection_timeout_seconds
    response = templates.TemplateResponse(
        request=request, name="scan_mode.html",
        context={
            "csrf_token": token, "classroom": classroom,
            "selected": selected, "result": result, "outcome": outcome,
            "confirmation": confirmation, "error": error,
            "request_id": uuid.uuid4(),
            "selection_token": selected_token,
            "selection_expires_at": selection_expires_at,
            "selection_timeout_seconds": settings_row.student_selection_timeout_seconds,
            "sound_enabled": settings_row.sound_enabled,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token)
    return response


def _selected_uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError as exc:
        raise ScanValidationError("selected student identifier is invalid") from exc


def _require_selection(
    request: Request, db: Session, *,
    classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
    selection_token: str | None,
) -> None:
    settings_row = db.get(ClassroomSettings, classroom_id)
    if settings_row is None:
        raise HTTPException(status_code=404, detail="classroom settings not found")
    verify_selection_token(
        session_token=request.cookies.get(settings.session_cookie_name, ""),
        classroom_id=classroom_id, enrollment_id=enrollment_id,
        selection_token=selection_token,
        timeout_seconds=settings_row.student_selection_timeout_seconds,
    )


@router.get("")
def scan_page(
    classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db),
):
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    return _page(request, db=db, classroom=access.classroom)


@router.post("/resolve")
def scan_preview(
    classroom_id: uuid.UUID, request: Request,
    payload: str = Form(...), selected_enrollment_id: str | None = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db),
):
    """Retain an explicitly read-only resolution endpoint for previews."""
    validate_csrf(request, csrf_token)
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        _, result = resolve_scan(
            db, user_id=user.id, classroom_id=classroom_id,
            payload=payload, selected_enrollment_id=_selected_uuid(selected_enrollment_id),
        )
    except ScanValidationError as exc:
        return _page(request, db=db, classroom=access.classroom, error=str(exc), status_code=400)
    return _page(request, db=db, classroom=access.classroom,
                 selected=result.enrollment, result=result,
                 selection_valid=result.command == "STUDENT")


def _post_action(
    *, db: Session, request: Request, classroom_id: uuid.UUID,
    access, user_id: uuid.UUID, payload: str, enrollment_id: uuid.UUID,
    request_id: uuid.UUID, expected_action_id: uuid.UUID | None = None,
    expected_quick_action_id: uuid.UUID | None = None,
    expected_amount: int | None = None,
):
    try:
        outcome = post_scanned_action(
            db, actor_user_id=user_id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, payload=payload, request_id=request_id,
            expected_action_id=expected_action_id,
            expected_quick_action_id=expected_quick_action_id,
            expected_amount=expected_amount,
        )
        db.commit()
    except ClassroomNotFoundError:
        db.rollback()
        raise HTTPException(status_code=404, detail="classroom not found")
    except ClassroomPermissionError:
        db.rollback()
        raise HTTPException(status_code=403, detail="permission denied")
    except FinancialIdempotencyConflictError:
        db.rollback()
        raise HTTPException(status_code=409, detail="request ID reused for another scan")
    except ScanValidationError as exc:
        db.rollback()
        return _page(request, db=db, classroom=access.classroom, error=str(exc), status_code=400)
    except Exception:
        db.rollback()
        raise
    selected = db.get(Enrollment, enrollment_id)
    return _page(
        request, db=db, classroom=access.classroom, selected=selected, outcome=outcome, selection_valid=True,
        status_code=422 if outcome.rejection_code else 200,
    )


@router.post("/execute")
def scan_execute(
    classroom_id: uuid.UUID, request: Request,
    payload: str = Form(...), selected_enrollment_id: str | None = Form(None),
    selection_token: str | None = Form(None),
    request_id: uuid.UUID = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        command, _ = parse_scan_command(payload)
        selected_id = _selected_uuid(selected_enrollment_id)
    except ScanValidationError as exc:
        return _page(request, db=db, classroom=access.classroom, error=str(exc), status_code=400)

    if command == "STUDENT":
        try:
            _, result = resolve_scan(
                db, user_id=user.id, classroom_id=classroom_id,
                payload=payload, selected_enrollment_id=selected_id,
            )
        except ScanValidationError as exc:
            return _page(request, db=db, classroom=access.classroom,
                         error=str(exc), status_code=400)
        return _page(request, db=db, classroom=access.classroom,
                     selected=result.enrollment, result=result, selection_valid=True)

    if selected_id is None:
        return _page(request, db=db, classroom=access.classroom,
                     error="scan a student before scanning a Quick Action or Action",
                     status_code=400)

    try:
        _require_selection(
            request, db, classroom_id=classroom_id,
            enrollment_id=selected_id, selection_token=selection_token,
        )
    except SelectionExpiredError as exc:
        return _page(request, db=db, classroom=access.classroom,
                     error=str(exc), status_code=409)

    # Previously committed requests must replay even if an action was later
    # renamed, disabled, or remapped. The service checks its exact fingerprint.
    previous = db.scalar(select(FinancialRequest.id).where(
        FinancialRequest.classroom_id == classroom_id,
        FinancialRequest.idempotency_key == str(request_id),
    ))
    classroom_settings = db.get(ClassroomSettings, classroom_id)
    if classroom_settings is None:
        raise HTTPException(status_code=404, detail="classroom settings not found")
    if previous is None and classroom_settings.confirm_normal_transactions:
        try:
            _, result = resolve_scan(
                db, user_id=user.id, classroom_id=classroom_id,
                payload=payload, selected_enrollment_id=selected_id,
            )
        except ScanValidationError as exc:
            return _page(request, db=db, classroom=access.classroom, error=str(exc), status_code=400)
        if result.action.action_kind == "general":
            return _page(
                request, db=db, classroom=access.classroom, selected=result.enrollment,
                result=result, selection_valid=True, confirmation={
                    "payload": payload,
                    "request_id": request_id,
                    "action_id": result.action.id,
                    "quick_id": result.quick_action.id if result.quick_action else None,
                    "amount": result.action.amount,
                },
            )
    return _post_action(
        db=db, request=request, classroom_id=classroom_id, access=access,
        user_id=user.id, payload=payload, enrollment_id=selected_id,
        request_id=request_id,
    )


@router.post("/confirm")
def scan_confirm(
    classroom_id: uuid.UUID, request: Request,
    payload: str = Form(...), selected_enrollment_id: uuid.UUID = Form(...),
    request_id: uuid.UUID = Form(...), expected_action_id: uuid.UUID = Form(...),
    expected_quick_action_id: uuid.UUID | None = Form(None),
    expected_amount: int = Form(...), csrf_token: str = Form(...),
    selection_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    user, access = _context(request, db, classroom_id)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        _require_selection(
            request, db, classroom_id=classroom_id,
            enrollment_id=selected_enrollment_id, selection_token=selection_token,
        )
    except SelectionExpiredError as exc:
        return _page(request, db=db, classroom=access.classroom,
                     error=str(exc), status_code=409)
    return _post_action(
        db=db, request=request, classroom_id=classroom_id, access=access,
        user_id=user.id, payload=payload, enrollment_id=selected_enrollment_id,
        request_id=request_id, expected_action_id=expected_action_id,
        expected_quick_action_id=expected_quick_action_id,
        expected_amount=expected_amount,
    )
