from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth import User
from app.services.auth import resolve_user_session


def current_user_from_request(request: Request, db: Session) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    return resolve_user_session(db, token=token)
