from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.database import get_db
from app.models.action import Action, QuickAction
from app.models.student import Enrollment
from app.services.classrooms import ClassroomNotFoundError, require_classroom_access
from app.services.qr_codes import FORMATS, qr_command, render_qr
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


@router.get("")
def qr_center(classroom_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    access = _access(request, db, classroom_id)
    if access is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

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

    return templates.TemplateResponse(request=request, name="qr_center.html", context={
        "classroom": access.classroom,
        "students": students,
        "quick_actions": quick_actions,
        "actions": actions,
    }, headers={"Cache-Control": "private, no-store"})


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
