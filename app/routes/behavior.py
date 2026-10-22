from __future__ import annotations

import re
import uuid
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.database import SessionLocal, get_db
from app.services.behavior_exports import iter_classroom_behavior_csv
from app.services.behavior_history import (
    BEHAVIOR_TYPES,
    BehaviorHistoryEnrollmentNotFoundError,
    BehaviorHistoryValidationError,
    list_classroom_behavior,
    select_classroom_behavior,
)
from app.services.classrooms import ClassroomNotFoundError
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/behavior", tags=["behavior"])


def _date_filter(raw: str, label: str) -> date | None:
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise BehaviorHistoryValidationError(f"{label} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BehaviorHistoryValidationError(f"{label} must be a valid date") from exc


@router.get("")
def classroom_behavior_history(
    classroom_id: uuid.UUID,
    request: Request,
    enrollment_id: uuid.UUID | None = None,
    behavior_type: str = "",
    date_from: str = "",
    date_to: str = "",
    before: str = "",
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        page = list_classroom_behavior(
            db, user_id=user.id, classroom_id=classroom_id,
            enrollment_id=enrollment_id, behavior_type=behavior_type,
            date_from=_date_filter(date_from, "start date"),
            date_to=_date_filter(date_to, "end date"),
            before=before,
        )
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    except BehaviorHistoryEnrollmentNotFoundError:
        raise HTTPException(status_code=404, detail="student enrollment not found")
    except BehaviorHistoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    selected = {
        "enrollment_id": str(enrollment_id) if enrollment_id else "",
        "behavior_type": behavior_type,
        "date_from": date_from,
        "date_to": date_to,
    }
    filter_query = {key: val for key, val in selected.items() if val}
    export_url = f"/classrooms/{classroom_id}/behavior/export.csv"
    if filter_query:
        export_url += "?" + urlencode(filter_query)

    next_url = None
    if page.next_before is not None:
        query = {key: val for key, val in selected.items() if val}
        query["before"] = page.next_before
        next_url = f"/classrooms/{classroom_id}/behavior?" + urlencode(query)

    return templates.TemplateResponse(
        request=request,
        name="behavior_history.html",
        context={
            "classroom": page.access.classroom,
            "roster": page.roster,
            "rows": page.rows,
            "positive_count": page.positive_count,
            "negative_count": page.negative_count,
            "total_count": page.positive_count + page.negative_count,
            "behavior_types": BEHAVIOR_TYPES,
            "selected": selected,
            "next_url": next_url,
            "export_url": export_url,
            "timezone_name": page.timezone_name,
        },
    )


@router.get("/export.csv")
def classroom_behavior_csv(
    classroom_id: uuid.UUID,
    request: Request,
    enrollment_id: uuid.UUID | None = None,
    behavior_type: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    filters = {
        "user_id": user.id,
        "classroom_id": classroom_id,
        "enrollment_id": enrollment_id,
        "behavior_type": behavior_type,
    }
    try:
        filters["date_from"] = _date_filter(date_from, "start date")
        filters["date_to"] = _date_filter(date_to, "end date")
        # Reject unauthorized/invalid requests before streaming response headers.
        select_classroom_behavior(db, **filters)
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    except BehaviorHistoryEnrollmentNotFoundError:
        raise HTTPException(status_code=404, detail="student enrollment not found")
    except BehaviorHistoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def generate_csv():
        # Streaming outlives the request session; re-authorize inside a dedicated
        # read-only, repeatable-read transaction for an internally consistent export.
        with SessionLocal() as export_db, export_db.begin():
            export_db.execute(text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            ))
            selection = select_classroom_behavior(export_db, **filters)
            yield from iter_classroom_behavior_csv(export_db, selection)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="qrbank-classroom-{classroom_id}-behavior.csv"'
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
