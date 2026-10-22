from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthToken
from app.services.auth import (
    InvalidVerificationTokenError,
    create_email_verification_token,
    register_user,
    verify_email_token,
)


def test_verification_tokens_are_hashed_and_resend_invalidates_old_link(engine) -> None:
    email = f"verify-{uuid.uuid4()}@example.test"

    with Session(engine) as db, db.begin():
        user = register_user(
            db,
            email=email,
            password="verification-password",
        )
        first = create_email_verification_token(db, user_id=user.id)
        second = create_email_verification_token(db, user_id=user.id)
        user_id = user.id

        record = db.scalar(
            select(AuthToken).where(
                AuthToken.user_id == user_id,
                AuthToken.token_hash.is_not(None),
                AuthToken.consumed_at.is_(None),
            )
        )
        assert record is not None
        assert record.token_hash != second.token.encode("utf-8")
        assert len(record.token_hash) == 32

    with Session(engine) as db, db.begin():
        with pytest.raises(InvalidVerificationTokenError):
            verify_email_token(db, token=first.token)

    with Session(engine) as db, db.begin():
        verified = verify_email_token(db, token=second.token)
        assert verified.id == user_id
        assert verified.account_status == "active"
        assert verified.email_verified_at is not None

    with Session(engine) as db, db.begin():
        with pytest.raises(InvalidVerificationTokenError):
            verify_email_token(db, token=second.token)
