from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.services.auth import (
    InvalidCredentialsError,
    authenticate_user,
    create_user_session,
    register_user,
    resolve_user_session,
    revoke_user_session,
)
from app.services.classrooms import create_classroom_for_owner


def test_register_authenticate_and_server_side_session(engine) -> None:
    email = "Teacher.One@Example.Test"
    password = "classroom-bank-password"

    with Session(engine) as db, db.begin():
        user = register_user(db, email=email, password=password)
        user_id = user.id
        assert user.email_normalized == email.casefold()
        assert user.password_hash != password
        assert user.account_status == "pending_verification"

    with Session(engine) as db:
        authenticated = authenticate_user(db, email="teacher.one@example.test", password=password)
        assert authenticated.id == user_id

        try:
            authenticate_user(db, email=email, password="not-the-password")
        except InvalidCredentialsError:
            pass
        else:
            raise AssertionError("wrong password should be rejected")

    with Session(engine) as db, db.begin():
        created = create_user_session(db, user_id=user_id, user_agent="pytest")
        raw_token = created.token

    with Session(engine) as db:
        resolved = resolve_user_session(db, token=raw_token)
        assert resolved is not None
        assert resolved.id == user_id

    with Session(engine) as db, db.begin():
        assert revoke_user_session(db, token=raw_token)

    with Session(engine) as db:
        assert resolve_user_session(db, token=raw_token) is None


def test_create_classroom_creates_owner_membership_and_zero_default_settings(engine) -> None:
    with Session(engine) as db, db.begin():
        owner = register_user(
            db,
            email="owner.classroom@example.test",
            password="classroom-owner-password",
        )
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner.id,
            name="Period 1 Math",
            school_year="2026-2027",
        )
        classroom_id = classroom.id
        owner_id = owner.id

    with Session(engine) as db:
        membership = db.scalar(
            select(ClassroomMembership).where(
                ClassroomMembership.classroom_id == classroom_id,
                ClassroomMembership.user_id == owner_id,
            )
        )
        settings = db.get(ClassroomSettings, classroom_id)
        stored_user = db.get(User, owner_id)

        assert stored_user is not None
        assert membership is not None
        assert membership.role == "owner"
        assert settings is not None
        assert settings.starting_balance == 0
        assert settings.allow_negative_balance is False
