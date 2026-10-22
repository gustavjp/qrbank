from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.models.action import Action, QuickAction
from app.models.student import Enrollment
from app.services.classrooms import ClassroomNotFoundError, require_classroom_access
from app.services.qr_archives import ArchiveQr, MAX_ARCHIVE_ITEMS, build_qr_archive
from app.services.qr_codes import FORMATS, qr_command, render_qr
from app.services.qr_printing import (
    LAYOUTS, MAX_PRINT_ITEMS, PAPERS, PrintableQr, generate_printable_pdf,
)
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/qr", tags=["qr_codes"])


def _access(request: Request, db: Session, classroom_id: uuid.UUID):
    user = current_user_from_request(request, db)
    if user is None:
        return None
    try:
        return require_classroom_access(db, user_id=user.id, classroom_id=classroom_id)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail="classroom not found") from exc


def _catalog(db: Session, classroom_id: uuid.UUID):
    students = db.scalars(select(Enrollment).where(
        Enrollment.classroom_id == classroom_id,
        Enrollment.active.is_(True),
    ).order_by(Enrollment.student_code)).all()
    quick_actions = db.execute(select(QuickAction, Action).join(Action, and_(
        QuickAction.action_id == Action.id,
        QuickAction.classroom_id == Action.classroom_id,
    )).where(
        QuickAction.classroom_id == classroom_id,
        QuickAction.active.is_(True),
        Action.active.is_(True),
    ).order_by(QuickAction.display_order, QuickAction.quick_action_code)).all()
    actions = db.scalars(select(Action).where(
        Action.classroom_id == classroom_id,
        Action.active.is_(True),
    ).order_by(Action.action_code)).all()
    return students, quick_actions, actions


def _selected_ids(selection, student_ids, quick_ids, action_ids,
                  students, quick_actions, actions):
    active_students = {row.id for row in students}
    active_quick = {quick.id for quick, _ in quick_actions}
    active_actions = {row.id for row in actions}
    if selection == "all":
        return active_students, active_quick, active_actions
    wanted_students = set(student_ids)
    wanted_quick = set(quick_ids)
    wanted_actions = set(action_ids)
    if (not wanted_students.issubset(active_students)
        or not wanted_quick.issubset(active_quick)
        or not wanted_actions.issubset(active_actions)):
        raise HTTPException(status_code=404, detail="selected QR entity not found")
    return wanted_students, wanted_quick, wanted_actions


@router.get("")
def qr_center(classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    access = _access(request, db, classroom_id)
    if access is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    students, quick_actions, actions = _catalog(db, classroom_id)

    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(request=request, name="qr_center.html", context={
        "classroom": access.classroom, "csrf_token": csrf_token,
        "students": students, "quick_actions": quick_actions, "actions": actions,
    }, headers={"Cache-Control": "private, no-store"})
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/print.pdf")
def qr_print_pdf(
    classroom_id: uuid.UUID, request: Request,
    csrf_token: str = Form(...), paper: str = Form(...), layout: str = Form(...),
    selection: str = Form(...), student_ids: list[uuid.UUID] = Form([]),
    quick_ids: list[uuid.UUID] = Form([]), action_ids: list[uuid.UUID] = Form([]),
    db: Session = Depends(get_db),
):
    access = _access(request, db, classroom_id)
    if access is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    validate_csrf(request, csrf_token)
    if paper not in PAPERS or layout not in LAYOUTS or selection not in {"all", "selected"}:
        raise HTTPException(status_code=400, detail="invalid QR print settings")
    students, quick_actions, actions = _catalog(db, classroom_id)

    wanted_students, wanted_quick, wanted_actions = _selected_ids(
        selection, student_ids, quick_ids, action_ids, students, quick_actions, actions,
    )

    items: list[PrintableQr] = []
    if layout in {"qr_only", "student_cards"}:
        items.extend(PrintableQr("student", row.student_code,
                                 f"Student {row.student_code}")
                     for row in students if row.id in wanted_students)
    if layout in {"qr_only", "quick_action_cards"}:
        items.extend(PrintableQr("quick", quick.quick_action_code,
                                 f"Quick {quick.quick_action_code}: {action.name}")
                     for quick, action in quick_actions if quick.id in wanted_quick)
    if layout == "qr_only":
        items.extend(PrintableQr("action", row.action_code,
                                 f"Action {row.action_code}: {row.name}")
                     for row in actions if row.id in wanted_actions)

    if not 1 <= len(items) <= MAX_PRINT_ITEMS:
        raise HTTPException(status_code=400, detail=(
            f"Select between 1 and {MAX_PRINT_ITEMS} active QR codes for this layout"
        ))
    pdf = generate_printable_pdf(
        items, classroom_name=access.classroom.name,
        school_year=access.classroom.school_year, paper=paper, layout=layout,
    )
    return Response(
        content=pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="qrbank-{layout}-{paper}.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/export.zip")
def qr_export_zip(
    classroom_id: uuid.UUID, request: Request,
    csrf_token: str = Form(...), image_format: str = Form(...),
    selection: str = Form(...), student_ids: list[uuid.UUID] = Form([]),
    quick_ids: list[uuid.UUID] = Form([]), action_ids: list[uuid.UUID] = Form([]),
    db: Session = Depends(get_db),
):
    access = _access(request, db, classroom_id)
    if access is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    validate_csrf(request, csrf_token)
    if image_format not in FORMATS or selection not in {"all", "selected"}:
        raise HTTPException(status_code=400, detail="invalid QR ZIP settings")

    students, quick_actions, actions = _catalog(db, classroom_id)
    wanted_students, wanted_quick, wanted_actions = _selected_ids(
        selection, student_ids, quick_ids, action_ids, students, quick_actions, actions,
    )
    items = [ArchiveQr("student", row.id, row.student_code,
                       f"Student {row.student_code}")
             for row in students if row.id in wanted_students]
    items.extend(ArchiveQr("quick", quick.id, quick.quick_action_code,
                           f"Quick {quick.quick_action_code}: {action.name}")
                 for quick, action in quick_actions if quick.id in wanted_quick)
    items.extend(ArchiveQr("action", row.id, row.action_code,
                           f"Action {row.action_code}: {row.name}")
                 for row in actions if row.id in wanted_actions)
    if not 1 <= len(items) <= MAX_ARCHIVE_ITEMS:
        raise HTTPException(status_code=400, detail=(
            f"Select between 1 and {MAX_ARCHIVE_ITEMS} active QR codes for ZIP export"
        ))

    return Response(
        content=build_qr_archive(
            items, image_format=image_format,
            classroom_name=access.classroom.name,
            school_year=access.classroom.school_year,
        ),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="qrbank-qr-{image_format}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{kind}/{entity_id}/{image_format}")
def qr_image(
    classroom_id: uuid.UUID, kind: str, entity_id: uuid.UUID, image_format: str,
    request: Request, download: bool = False, db: Session = Depends(get_db),
):
    access = _access(request, db, classroom_id)
    if access is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    if image_format not in FORMATS:
        raise HTTPException(status_code=404, detail="QR format not found")

    if kind == "student":
        code = db.scalar(select(Enrollment.student_code).where(
            Enrollment.id == entity_id,
            Enrollment.classroom_id == classroom_id,
            Enrollment.active.is_(True),
        ))
    elif kind == "quick":
        code = db.scalar(select(QuickAction.quick_action_code).join(Action, and_(
            QuickAction.action_id == Action.id,
            QuickAction.classroom_id == Action.classroom_id,
        )).where(
            QuickAction.id == entity_id,
            QuickAction.classroom_id == classroom_id,
            QuickAction.active.is_(True),
            Action.active.is_(True),
        ))
    elif kind == "action":
        code = db.scalar(select(Action.action_code).where(
            Action.id == entity_id,
            Action.classroom_id == classroom_id,
            Action.active.is_(True),
        ))
    else:
        raise HTTPException(status_code=404, detail="QR entity type not found")

    if code is None:
        raise HTTPException(status_code=404, detail="active QR entity not found")
    payload = qr_command(kind, code)
    filename = f"qrbank-{kind}-{code.lower()}.{image_format}"
    return Response(
        content=render_qr(payload, image_format), media_type=FORMATS[image_format],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'{"attachment" if download else "inline"}; filename="{filename}"'
            ),
        },
    )
