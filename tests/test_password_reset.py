from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.email_delivery import build_password_reset_url
from app.main import app
from app.models.auth import AuthToken, User, UserSession
from app.security import hash_one_time_token, verify_password
from app.services.auth import (
    InvalidCredentialsError,
    InvalidPasswordResetTokenError,
    WeakPasswordError,
    authenticate_user,
    create_email_verification_token,
    create_password_reset_token,
    create_user_session,
    register_user,
    reset_password_with_token,
    verify_email_token,
)


def _active_user(db: Session) -> User:
    user = register_user(
        db,
        email=f"reset-{uuid.uuid4()}@example.test",
        password="original-password-123",
    )
    verification = create_email_verification_token(db, user_id=user.id)
    verify_email_token(db, token=verification.token)
    return user


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(settings.csrf_cookie_name)
    assert value
    return value


def test_reset_token_is_hashed_single_use_and_revokes_all_sessions(engine) -> None:
    with Session(engine) as db, db.begin():
        user = _active_user(db)
        existing = create_user_session(db, user_id=user.id)
        created = create_password_reset_token(db, email=user.email)
        assert created is not None
        user_id = user.id
        record = db.scalar(
            select(AuthToken).where(
                AuthToken.token_hash == hash_one_time_token(created.token)
            )
        )
        assert record is not None
        assert record.token_type == "password_reset"
        assert record.token_hash != created.token.encode()
        assert len(record.token_hash) == 32

    with Session(engine) as db, db.begin():
        changed = reset_password_with_token(
            db, token=created.token, password="replacement-password-456"
        )
        assert changed.id == user_id

    with Session(engine) as db:
        user = db.get(User, user_id)
        assert user is not None
        assert verify_password("replacement-password-456", user.password_hash)
        assert not verify_password("original-password-123", user.password_hash)
        sessions = db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
        assert len(sessions) == 1
        assert sessions[0].revoked_at is not None
        with pytest.raises(InvalidCredentialsError):
            authenticate_user(db, email=user.email, password="original-password-123")
        authenticate_user(db, email=user.email, password="replacement-password-456")

    with Session(engine) as db:
        with pytest.raises(InvalidPasswordResetTokenError):
            reset_password_with_token(
                db, token=created.token, password="another-valid-password"
            )


def test_reset_cooldown_and_reissued_link_invalidates_previous(engine) -> None:
    with Session(engine) as db, db.begin():
        user = _active_user(db)
        first = create_password_reset_token(db, email=user.email)
        assert first is not None
        assert create_password_reset_token(db, email=user.email) is None
        db.execute(
            update(AuthToken)
            .where(AuthToken.token_hash == hash_one_time_token(first.token))
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=2))
        )
        second = create_password_reset_token(db, email=user.email)
        assert second is not None
        assert first.token != second.token

    with Session(engine) as db:
        with pytest.raises(InvalidPasswordResetTokenError):
            reset_password_with_token(db, token=first.token, password="new-password-123456")
        assert reset_password_with_token(
            db, token=second.token, password="new-password-123456"
        ) is not None
        db.rollback()


def test_expired_and_invalid_tokens_rejected_and_weak_password_preserves_link(engine) -> None:
    with Session(engine) as db, db.begin():
        user = _active_user(db)
        created = create_password_reset_token(db, email=user.email)
        assert created is not None

    with Session(engine) as db:
        with pytest.raises(WeakPasswordError):
            reset_password_with_token(db, token=created.token, password="short")
        with pytest.raises(InvalidPasswordResetTokenError):
            reset_password_with_token(db, token="not-a-real-link", password="good-password-123")

    with Session(engine) as db, db.begin():
        db.execute(
            update(AuthToken)
            .where(AuthToken.token_hash == hash_one_time_token(created.token))
            .values(
                created_at=datetime.now(timezone.utc) - timedelta(hours=2),
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )

    with Session(engine) as db:
        with pytest.raises(InvalidPasswordResetTokenError):
            reset_password_with_token(db, token=created.token, password="good-password-123")


def test_pending_and_suspended_accounts_do_not_get_reset_links(engine) -> None:
    with Session(engine) as db, db.begin():
        pending = register_user(
            db,
            email=f"pending-reset-{uuid.uuid4()}@example.test",
            password="long-enough-password",
        )
        suspended = _active_user(db)
        suspended.account_status = "suspended"
        assert create_password_reset_token(db, email=pending.email) is None
        assert create_password_reset_token(db, email=suspended.email) is None
        assert create_password_reset_token(db, email="nobody@example.test") is None


def test_password_reset_http_flow_csrf_and_non_enumeration(engine, monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.routes.auth.send_password_reset_email",
        lambda *, email, token: sent.append((email, token)),
    )
    client = TestClient(app, follow_redirects=False)
    client.get("/auth/forgot-password")

    invalid_csrf = client.post(
        "/auth/forgot-password",
        data={"email": "unknown@example.test", "csrf_token": "invalid"},
    )
    assert invalid_csrf.status_code == 403

    unknown = client.post(
        "/auth/forgot-password",
        data={"email": "unknown@example.test", "csrf_token": _csrf(client)},
    )
    assert unknown.status_code == 303
    assert unknown.headers["location"] == "/auth/reset-requested"
    assert not sent

    with Session(engine) as db, db.begin():
        user = _active_user(db)
        email = user.email

    known = client.post(
        "/auth/forgot-password",
        data={"email": email, "csrf_token": _csrf(client)},
    )
    assert known.status_code == unknown.status_code
    assert known.headers["location"] == unknown.headers["location"]
    assert len(sent) == 1
    assert sent[0][0] == email
    token = sent[0][1]

    form = client.get("/auth/reset-password", params={"token": token})
    assert form.status_code == 200
    assert form.headers["cache-control"] == "no-store"
    assert form.headers["referrer-policy"] == "no-referrer"

    wrong_csrf = client.post(
        "/auth/reset-password",
        data={
            "token": token,
            "password": "replacement-password-123",
            "password_confirm": "replacement-password-123",
            "csrf_token": "invalid",
        },
    )
    assert wrong_csrf.status_code == 403

    mismatch = client.post(
        "/auth/reset-password",
        data={
            "token": token,
            "password": "replacement-password-123",
            "password_confirm": "different-password-123",
            "csrf_token": _csrf(client),
        },
    )
    assert mismatch.status_code == 400
    assert "Passwords do not match" in mismatch.text

    success = client.post(
        "/auth/reset-password",
        data={
            "token": token,
            "password": "replacement-password-123",
            "password_confirm": "replacement-password-123",
            "csrf_token": _csrf(client),
        },
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/auth/login?reset=1"
    assert "Password updated" in client.get("/auth/login?reset=1").text

    replay = client.post(
        "/auth/reset-password",
        data={
            "token": token,
            "password": "replacement-password-123",
            "password_confirm": "replacement-password-123",
            "csrf_token": _csrf(client),
        },
    )
    assert replay.status_code == 400
    assert "cannot be used" in replay.text


def test_password_reset_url_uses_public_base_url(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.email_delivery.settings",
        SimpleNamespace(public_base_url="https://school.example.test"),
    )
    assert build_password_reset_url("hello +/?") == (
        "https://school.example.test/auth/reset-password?token=hello%20%2B%2F%3F"
    )


def test_password_reset_smtp_failure_remains_non_enumerating(engine, monkeypatch) -> None:
    from app.email_delivery import EmailDeliveryFailed

    monkeypatch.setattr("app.routes.auth.email_delivery_is_available", lambda: True)

    def fail_delivery(*, email: str, token: str) -> None:
        raise EmailDeliveryFailed("temporary SMTP failure")

    monkeypatch.setattr("app.routes.auth.send_password_reset_email", fail_delivery)
    client = TestClient(app, follow_redirects=False)
    client.get("/auth/forgot-password")

    unknown = client.post(
        "/auth/forgot-password",
        data={"email": "not-registered@example.test", "csrf_token": _csrf(client)},
    )

    with Session(engine) as db, db.begin():
        user = _active_user(db)
        known_email = user.email

    known = client.post(
        "/auth/forgot-password",
        data={"email": known_email, "csrf_token": _csrf(client)},
    )
    assert known.status_code == unknown.status_code == 303
    assert known.headers["location"] == unknown.headers["location"] == "/auth/reset-requested"


def test_resend_verification_smtp_failure_remains_non_enumerating(engine, monkeypatch) -> None:
    from app.email_delivery import EmailDeliveryFailed

    monkeypatch.setattr("app.routes.auth.email_delivery_is_available", lambda: True)

    def fail_delivery(*, email: str, token: str) -> None:
        raise EmailDeliveryFailed("temporary SMTP failure")

    monkeypatch.setattr("app.routes.auth.send_verification_email", fail_delivery)
    client = TestClient(app, follow_redirects=False)
    client.get("/auth/register")

    unknown = client.post(
        "/auth/resend-verification",
        data={"email": "not-registered@example.test", "csrf_token": _csrf(client)},
    )

    with Session(engine) as db, db.begin():
        pending = register_user(
            db,
            email=f"pending-smtp-{uuid.uuid4()}@example.test",
            password="valid-password-123",
        )
        known_email = pending.email

    known = client.post(
        "/auth/resend-verification",
        data={"email": known_email, "csrf_token": _csrf(client)},
    )
    assert known.status_code == unknown.status_code == 303
    assert known.headers["location"] == unknown.headers["location"] == "/auth/check-email"
