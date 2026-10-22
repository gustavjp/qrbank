from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth import User, UserSession
from app.security import generate_session_token, hash_password, hash_session_token, verify_password


class DuplicateEmailError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


@dataclass(frozen=True)
class CreatedSession:
    token: str
    expires_at: datetime


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized:
        raise ValueError("a valid email address is required")
    return normalized


def register_user(db: Session, *, email: str, password: str) -> User:
    normalized = normalize_email(email)
    existing = db.scalar(select(User.id).where(User.email_normalized == normalized))
    if existing is not None:
        raise DuplicateEmailError("an account with that email already exists")

    user = User(
        id=uuid.uuid4(),
        email=email.strip(),
        email_normalized=normalized,
        password_hash=hash_password(password),
        account_status="pending_verification",
        teacher_verification_status="not_requested",
    )
    db.add(user)
    db.flush()
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User:
    normalized = normalize_email(email)
    user = db.scalar(select(User).where(User.email_normalized == normalized))

    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentialsError("invalid email or password")

    if user.account_status in {"suspended", "disabled"}:
        raise InvalidCredentialsError("account is not available")

    return user


def create_user_session(
    db: Session,
    *,
    user_id: uuid.UUID,
    user_agent: str | None = None,
    ttl_seconds: int | None = None,
) -> CreatedSession:
    raw_token = generate_session_token()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds or settings.session_ttl_seconds)

    record = UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token(raw_token),
        expires_at=expires_at,
        last_seen_at=now,
        user_agent=user_agent,
    )
    db.add(record)
    db.flush()
    return CreatedSession(token=raw_token, expires_at=expires_at)


def resolve_user_session(db: Session, *, token: str) -> User | None:
    now = datetime.now(timezone.utc)
    token_hash = hash_session_token(token)

    return db.scalar(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.session_token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            User.account_status.not_in({"suspended", "disabled"}),
        )
    )


def revoke_user_session(db: Session, *, token: str) -> bool:
    record = db.scalar(
        select(UserSession).where(
            UserSession.session_token_hash == hash_session_token(token),
            UserSession.revoked_at.is_(None),
        )
    )
    if record is None:
        return False

    record.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return True
