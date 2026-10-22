from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth import AuthToken, User, UserSession
from app.security import (
    generate_one_time_token,
    generate_session_token,
    hash_one_time_token,
    hash_password,
    hash_session_token,
    verify_password,
)


class DuplicateEmailError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


class EmailVerificationRequiredError(ValueError):
    pass


class InvalidVerificationTokenError(ValueError):
    pass


class InvalidPasswordResetTokenError(ValueError):
    pass


class WeakPasswordError(ValueError):
    pass


@dataclass(frozen=True)
class CreatedSession:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class CreatedVerificationToken:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class CreatedPasswordResetToken:
    token: str
    expires_at: datetime
    recipient_email: str


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError("a valid email address is required")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise WeakPasswordError("password must be at least 12 characters")


def register_user(db: Session, *, email: str, password: str) -> User:
    normalized = normalize_email(email)
    validate_password(password)

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

    if user.email_verified_at is None or user.account_status == "pending_verification":
        raise EmailVerificationRequiredError("email verification is required")

    return user


def create_email_verification_token(
    db: Session,
    *,
    user_id: uuid.UUID,
    ttl_seconds: int | None = None,
) -> CreatedVerificationToken:
    now = datetime.now(timezone.utc)

    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user_id,
            AuthToken.token_type == "email_verification",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    raw_token = generate_one_time_token()
    expires_at = now + timedelta(
        seconds=ttl_seconds or settings.verification_token_ttl_seconds
    )

    db.add(
        AuthToken(
            id=uuid.uuid4(),
            user_id=user_id,
            token_type="email_verification",
            token_hash=hash_one_time_token(raw_token),
            expires_at=expires_at,
        )
    )
    db.flush()
    return CreatedVerificationToken(token=raw_token, expires_at=expires_at)


def verify_email_token(db: Session, *, token: str) -> User:
    now = datetime.now(timezone.utc)
    record = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_one_time_token(token),
            AuthToken.token_type == "email_verification",
            AuthToken.consumed_at.is_(None),
        )
    )

    if record is None or record.expires_at <= now:
        raise InvalidVerificationTokenError("verification link is invalid or expired")

    user = db.get(User, record.user_id)
    if user is None:
        raise InvalidVerificationTokenError("verification link is invalid")

    record.consumed_at = now
    user.email_verified_at = user.email_verified_at or now
    if user.account_status == "pending_verification":
        user.account_status = "active"

    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.token_type == "email_verification",
            AuthToken.consumed_at.is_(None),
            AuthToken.id != record.id,
        )
        .values(consumed_at=now)
    )
    db.flush()
    return user


def find_pending_verification_user(db: Session, *, email: str) -> User | None:
    normalized = normalize_email(email)
    return db.scalar(
        select(User).where(
            User.email_normalized == normalized,
            User.account_status == "pending_verification",
            User.email_verified_at.is_(None),
        )
    )


def create_password_reset_token(
    db: Session,
    *,
    email: str,
    ttl_seconds: int | None = None,
) -> CreatedPasswordResetToken | None:
    """Create one use-only token for an active, verified user.

    The same response is shown to the requester regardless of the return value.
    A short per-account cooldown limits repeated reset emails.
    """
    normalized = normalize_email(email)
    user = db.scalar(
        select(User).where(User.email_normalized == normalized).with_for_update()
    )
    if user is None or user.account_status != "active" or user.email_verified_at is None:
        return None

    now = datetime.now(timezone.utc)
    most_recent = db.scalar(
        select(AuthToken.created_at)
        .where(AuthToken.user_id == user.id, AuthToken.token_type == "password_reset")
        .order_by(AuthToken.created_at.desc())
        .limit(1)
    )
    if most_recent is not None and now - most_recent < timedelta(seconds=60):
        return None

    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.token_type == "password_reset",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw_token = generate_one_time_token()
    expires_at = now + timedelta(
        seconds=ttl_seconds if ttl_seconds is not None else settings.password_reset_token_ttl_seconds
    )
    db.add(
        AuthToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_type="password_reset",
            token_hash=hash_one_time_token(raw_token),
            expires_at=expires_at,
        )
    )
    db.flush()
    return CreatedPasswordResetToken(
        token=raw_token, expires_at=expires_at, recipient_email=user.email
    )


def reset_password_with_token(db: Session, *, token: str, password: str) -> User:
    """Consume a reset link and revoke every previously issued session atomically."""
    validate_password(password)
    now = datetime.now(timezone.utc)
    candidate = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_one_time_token(token),
            AuthToken.token_type == "password_reset",
        )
    )
    if candidate is None:
        raise InvalidPasswordResetTokenError("reset link is invalid or expired")

    # Match the user -> token lock order used by reset issuance to avoid deadlocks.
    user = db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    record = db.scalar(select(AuthToken).where(AuthToken.id == candidate.id).with_for_update())
    if (
        user is None
        or user.account_status != "active"
        or user.email_verified_at is None
        or record is None
        or record.consumed_at is not None
        or record.expires_at <= now
    ):
        raise InvalidPasswordResetTokenError("reset link is invalid or expired")

    user.password_hash = hash_password(password)
    user.updated_at = now
    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.token_type == "password_reset",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.flush()
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
            User.account_status == "active",
            User.email_verified_at.is_not(None),
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
