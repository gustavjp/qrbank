from __future__ import annotations

import re
import uuid
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth_context import current_user_from_request
from app.database import get_db
from app.services.classrooms import ClassroomNotFoundError
from app.services.transaction_history import (
    MANUAL_CATEGORIES,
    TRANSACTION_SOURCES,
    TRANSACTION_TYPES,
    TransactionHistoryEnrollmentNotFoundError,
    TransactionHistoryValidationError,
    list_classroom_transactions,
)
from app.web import templates


router = APIRouter(prefix="/classrooms/{classroom_id}/transactions", tags=["transactions"])


def _date_filter(raw: str, label: str) -> date | None:
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise TransactionHistoryValidationError(f"{label} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise TransactionHistoryValidationError(f"{label} must be a valid date") from exc


@router.get("")
def classroom_transactions(
    classroom_id: uuid.UUID,
    request: Request,
    enrollment_id: uuid.UUID | None = None,
    transaction_type: str = "",
    source: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    before: int | None = None,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        page = list_classroom_transactions(
            db,
            user_id=user.id,
            classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            transaction_type=transaction_type,
            source=source,
            category=category,
            date_from=_date_filter(date_from, "start date"),
            date_to=_date_filter(date_to, "end date"),
            before=before,
        )
    except ClassroomNotFoundError:
        raise HTTPException(status_code=404, detail="classroom not found")
    except TransactionHistoryEnrollmentNotFoundError:
        raise HTTPException(status_code=404, detail="student enrollment not found")
    except TransactionHistoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    selected = {
        "enrollment_id": str(enrollment_id) if enrollment_id else "",
        "transaction_type": transaction_type,
        "source": source,
        "category": category,
        "date_from": date_from,
        "date_to": date_to,
    }
    next_url = None
    if page.next_before is not None:
        query = {key: val for key, val in selected.items() if val}
        query["before"] = str(page.next_before)
        next_url = (
            f"/classrooms/{classroom_id}/transactions?" + urlencode(query)
        )

    return templates.TemplateResponse(
        request=request,
        name="transaction_history.html",
        context={
            "classroom": page.access.classroom,
            "rows": page.rows,
            "roster": page.roster,
            "selected": selected,
            "transaction_types": TRANSACTION_TYPES,
            "transaction_sources": TRANSACTION_SOURCES,
            "manual_categories": MANUAL_CATEGORIES,
            "next_url": next_url,
            "timezone_name": page.timezone_name,
        },
    )
