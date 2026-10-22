from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status
from starlette.responses import Response

from app.config import settings


def csrf_token_for_request(request: Request) -> str:
    existing = request.cookies.get(settings.csrf_cookie_name)
    return existing or secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=60 * 60 * 2,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def validate_csrf(request: Request, form_token: str) -> None:
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    if not cookie_token or not form_token or not hmac.compare_digest(cookie_token, form_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid CSRF token",
        )
