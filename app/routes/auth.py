from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.csrf import csrf_token_for_request, set_csrf_cookie, validate_csrf
from app.database import get_db
from app.email_delivery import EmailDeliveryNotConfigured, send_verification_email
from app.models.auth import User
from app.services.auth import (
    DuplicateEmailError,
    EmailVerificationRequiredError,
    InvalidCredentialsError,
    InvalidVerificationTokenError,
    WeakPasswordError,
    authenticate_user,
    create_email_verification_token,
    create_user_session,
    find_pending_verification_user,
    register_user,
    resolve_user_session,
    revoke_user_session,
    verify_email_token,
)
from app.web import templates


router = APIRouter()


def _current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    return resolve_user_session(db, token=token)


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


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    if _current_user(request, db) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    if _current_user(request, db) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return _form_response(request, template_name="register.html")


@router.post("/auth/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)

    if password != password_confirm:
        return _form_response(
            request,
            template_name="register.html",
            context={"error": "Passwords do not match.", "email": email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = register_user(db, email=email, password=password)
        verification = create_email_verification_token(db, user_id=user.id)
        db.commit()
    except (DuplicateEmailError, WeakPasswordError, ValueError) as exc:
        db.rollback()
        return _form_response(
            request,
            template_name="register.html",
            context={"error": str(exc), "email": email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except IntegrityError:
        db.rollback()
        return _form_response(
            request,
            template_name="register.html",
            context={"error": "An account with that email already exists.", "email": email},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        send_verification_email(email=user.email, token=verification.token)
    except EmailDeliveryNotConfigured:
        return _form_response(
            request,
            template_name="email_delivery_error.html",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return RedirectResponse("/auth/check-email", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/check-email")
def check_email(request: Request):
    return _form_response(request, template_name="check_email.html")


@router.post("/auth/resend-verification")
def resend_verification(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)

    try:
        user = find_pending_verification_user(db, email=email)
    except ValueError:
        user = None

    if user is not None:
        verification = create_email_verification_token(db, user_id=user.id)
        db.commit()
        try:
            send_verification_email(email=user.email, token=verification.token)
        except EmailDeliveryNotConfigured:
            return _form_response(
                request,
                template_name="email_delivery_error.html",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    return RedirectResponse("/auth/check-email", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/verify-email")
def verify_email_page(request: Request, token: str):
    return _form_response(
        request,
        template_name="verify_email.html",
        context={"verification_token": token},
    )


@router.post("/auth/verify-email")
def verify_email(
    request: Request,
    token: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)

    try:
        verify_email_token(db, token=token)
        db.commit()
    except InvalidVerificationTokenError:
        db.rollback()
        return _form_response(
            request,
            template_name="verification_invalid.html",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(
        "/auth/login?verified=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/auth/login")
def login_page(request: Request, verified: int = 0, db: Session = Depends(get_db)):
    if _current_user(request, db) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return _form_response(
        request,
        template_name="login.html",
        context={"verified": bool(verified)},
    )


@router.post("/auth/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)

    try:
        user = authenticate_user(db, email=email, password=password)
    except EmailVerificationRequiredError:
        return _form_response(
            request,
            template_name="login.html",
            context={
                "error": "Verify your email before signing in.",
                "email": email,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    except (InvalidCredentialsError, ValueError):
        return _form_response(
            request,
            template_name="login.html",
            context={
                "error": "Invalid email or password.",
                "email": email,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    created = create_user_session(
        db,
        user_id=user.id,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()

    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=created.token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)

    token = request.cookies.get(settings.session_cookie_name)
    if token:
        revoke_user_session(db, token=token)
        db.commit()

    response = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if user is None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)

    return _form_response(
        request,
        template_name="dashboard.html",
        context={"user": user},
    )
