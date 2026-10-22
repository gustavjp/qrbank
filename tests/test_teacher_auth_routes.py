from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.auth import User, UserSession
from app.security import hash_session_token


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(settings.csrf_cookie_name)
    assert value
    return value


def test_registration_requires_valid_csrf(engine) -> None:
    client = TestClient(app, follow_redirects=False)
    client.get("/auth/register")

    response = client.post(
        "/auth/register",
        data={
            "email": f"csrf-{uuid.uuid4()}@example.test",
            "password": "long-enough-password",
            "password_confirm": "long-enough-password",
            "csrf_token": "wrong-token",
        },
    )

    assert response.status_code == 403


def test_teacher_registration_verification_login_and_logout(engine, monkeypatch) -> None:
    email = f"teacher-{uuid.uuid4()}@example.test"
    password = "teacher-auth-password"
    sent: dict[str, str] = {}

    def fake_send_verification_email(*, email: str, token: str) -> None:
        sent["email"] = email
        sent["token"] = token

    monkeypatch.setattr(
        "app.routes.auth.send_verification_email",
        fake_send_verification_email,
    )

    client = TestClient(app, follow_redirects=False)

    register_page = client.get("/auth/register")
    assert register_page.status_code == 200

    registered = client.post(
        "/auth/register",
        data={
            "email": email,
            "password": password,
            "password_confirm": password,
            "csrf_token": _csrf(client),
        },
    )
    assert registered.status_code == 303
    assert registered.headers["location"] == "/auth/check-email"
    assert sent["email"] == email
    assert sent["token"]

    login_page = client.get("/auth/login")
    assert login_page.status_code == 200

    before_verification = client.post(
        "/auth/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )
    assert before_verification.status_code == 403
    assert "Verify your email" in before_verification.text

    verification_page = client.get(
        "/auth/verify-email",
        params={"token": sent["token"]},
    )
    assert verification_page.status_code == 200

    verified = client.post(
        "/auth/verify-email",
        data={
            "token": sent["token"],
            "csrf_token": _csrf(client),
        },
    )
    assert verified.status_code == 303
    assert verified.headers["location"] == "/auth/login?verified=1"

    client.get("/auth/login")
    logged_in = client.post(
        "/auth/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/dashboard"

    session_token = client.cookies.get(settings.session_cookie_name)
    assert session_token
    set_cookie = logged_in.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert email in dashboard.text

    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email_normalized == email.casefold()))
        assert user is not None
        assert user.account_status == "active"
        assert user.email_verified_at is not None

        session = db.scalar(
            select(UserSession).where(
                UserSession.session_token_hash == hash_session_token(session_token)
            )
        )
        assert session is not None
        assert session.revoked_at is None

    logged_out = client.post(
        "/auth/logout",
        data={"csrf_token": _csrf(client)},
    )
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/auth/login"
    assert client.cookies.get(settings.session_cookie_name) is None

    with Session(engine) as db:
        session = db.scalar(
            select(UserSession).where(
                UserSession.session_token_hash == hash_session_token(session_token)
            )
        )
        assert session is not None
        assert session.revoked_at is not None


def test_dashboard_redirects_without_valid_session(engine) -> None:
    client = TestClient(app, follow_redirects=False)

    response = client.get("/dashboard")

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
