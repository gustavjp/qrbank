from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner


PASSWORD = "classroom-management-password"


def _logged_in_client(engine, *, email: str | None = None) -> tuple[TestClient, uuid.UUID]:
    email = email or f"teacher-{uuid.uuid4()}@example.test"

    with Session(engine) as db, db.begin():
        user = register_user(db, email=email, password=PASSWORD)
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        created = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id = user.id
        session_token = created.token

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(settings.csrf_cookie_name)
    assert value
    return value


def test_create_classroom_creates_owner_and_selects_active_context(engine) -> None:
    client, user_id = _logged_in_client(engine)

    page = client.get("/classrooms/new")
    assert page.status_code == 200

    created = client.post(
        "/classrooms/new",
        data={
            "name": "Period 2 Science",
            "school_year": "2026-2027",
            "csrf_token": _csrf(client),
        },
    )
    assert created.status_code == 303
    assert created.headers["location"].startswith("/classrooms/")

    active_cookie = client.cookies.get(settings.active_classroom_cookie_name)
    assert active_cookie

    classroom_id = uuid.UUID(active_cookie)

    with Session(engine) as db:
        classroom = db.get(Classroom, classroom_id)
        classroom_settings = db.get(ClassroomSettings, classroom_id)
        membership = db.scalar(
            select(ClassroomMembership).where(
                ClassroomMembership.classroom_id == classroom_id,
                ClassroomMembership.user_id == user_id,
                ClassroomMembership.ended_at.is_(None),
            )
        )

        assert classroom is not None
        assert classroom.name == "Period 2 Science"
        assert classroom.school_year == "2026-2027"
        assert classroom_settings is not None
        assert classroom_settings.starting_balance == 0
        assert membership is not None
        assert membership.role == "owner"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Period 2 Science" in dashboard.text
    assert "Active classroom" in dashboard.text


def test_create_classroom_requires_valid_csrf(engine) -> None:
    client, _ = _logged_in_client(engine)

    response = client.post(
        "/classrooms/new",
        data={
            "name": "Should Not Exist",
            "school_year": "2026-2027",
            "csrf_token": "invalid",
        },
    )

    assert response.status_code == 403


def test_unrelated_user_cannot_view_or_select_classroom(engine) -> None:
    owner_client, owner_id = _logged_in_client(engine)
    other_client, _ = _logged_in_client(engine)

    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner_id,
            name="Owner Private Class",
            school_year="2026-2027",
        )
        classroom_id = classroom.id

    hidden = other_client.get(f"/classrooms/{classroom_id}")
    assert hidden.status_code == 404

    select_hidden = other_client.post(
        f"/classrooms/{classroom_id}/select",
        data={"csrf_token": _csrf(other_client)},
    )
    assert select_hidden.status_code == 404

    dashboard = other_client.get("/dashboard")
    assert "Owner Private Class" not in dashboard.text

    owner_dashboard = owner_client.get("/dashboard")
    assert "Owner Private Class" in owner_dashboard.text


def test_member_can_view_classroom_but_only_owner_can_change_settings(engine) -> None:
    owner_client, owner_id = _logged_in_client(engine)
    member_client, member_id = _logged_in_client(engine)

    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner_id,
            name="Shared Class",
            school_year="2026-2027",
        )
        classroom_id = classroom.id
        db.add(
            ClassroomMembership(
                id=uuid.uuid4(),
                classroom_id=classroom_id,
                user_id=member_id,
                role="teacher",
                created_by_user_id=owner_id,
            )
        )

    detail = member_client.get(f"/classrooms/{classroom_id}")
    assert detail.status_code == 200
    assert "Shared Class" in detail.text

    settings_page = member_client.get(f"/classrooms/{classroom_id}/settings")
    assert settings_page.status_code == 200
    assert "Only the classroom owner can change settings in V1." in settings_page.text

    forbidden = member_client.post(
        f"/classrooms/{classroom_id}/settings",
        data={
            "name": "Member Rename",
            "school_year": "2026-2027",
            "timezone": "America/New_York",
            "starting_balance": "50",
            "weekly_allowance_amount": "20",
            "weekly_allowance_day": "0",
            "weekly_allowance_time": "06:00",
            "weekly_wage_day": "0",
            "weekly_wage_time": "06:10",
            "duplicate_scan_seconds": "0",
            "student_selection_timeout_seconds": "120",
            "csrf_token": _csrf(member_client),
        },
    )
    assert forbidden.status_code == 403

    with Session(engine) as db:
        classroom = db.get(Classroom, classroom_id)
        assert classroom is not None
        assert classroom.name == "Shared Class"


def test_owner_can_update_classroom_settings_without_rewriting_balances(engine) -> None:
    client, owner_id = _logged_in_client(engine)

    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner_id,
            name="Before Update",
            school_year="2026-2027",
        )
        classroom_id = classroom.id

    page = client.get(f"/classrooms/{classroom_id}/settings")
    assert page.status_code == 200

    updated = client.post(
        f"/classrooms/{classroom_id}/settings",
        data={
            "name": "After Update",
            "school_year": "2027-2028",
            "timezone": "America/Chicago",
            "starting_balance": "75",
            "weekly_allowance_enabled": "1",
            "weekly_allowance_amount": "25",
            "weekly_allowance_day": "1",
            "weekly_allowance_time": "07:15",
            "weekly_wages_enabled": "1",
            "weekly_wage_day": "2",
            "weekly_wage_time": "07:20",
            "sound_enabled": "1",
            "duplicate_scan_seconds": "2",
            "allow_negative_balance": "1",
            "student_selection_timeout_seconds": "90",
            "confirm_normal_transactions": "1",
            "confirm_balance_reset": "1",
            "csrf_token": _csrf(client),
        },
    )
    assert updated.status_code == 303
    assert updated.headers["location"] == f"/classrooms/{classroom_id}/settings?saved=1"

    with Session(engine) as db:
        classroom = db.get(Classroom, classroom_id)
        classroom_settings = db.get(ClassroomSettings, classroom_id)

        assert classroom is not None
        assert classroom.name == "After Update"
        assert classroom.school_year == "2027-2028"

        assert classroom_settings is not None
        assert classroom_settings.timezone == "America/Chicago"
        assert classroom_settings.starting_balance == 75
        assert classroom_settings.weekly_allowance_enabled is True
        assert classroom_settings.weekly_allowance_amount == 25
        assert classroom_settings.weekly_allowance_day == 1
        assert classroom_settings.weekly_allowance_time.hour == 7
        assert classroom_settings.weekly_allowance_time.minute == 15
        assert classroom_settings.weekly_wages_enabled is True
        assert classroom_settings.allow_negative_balance is True
        assert classroom_settings.student_selection_timeout_seconds == 90
        assert classroom_settings.updated_by_user_id == owner_id

        audit = db.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.classroom_id == classroom_id,
                AdminAuditEvent.event_type == "classroom_settings_updated",
            )
        )
        assert audit is not None
        assert audit.actor_user_id == owner_id
        assert audit.target_id == classroom_id
        assert audit.before_json["name"] == "Before Update"
        assert audit.after_json["name"] == "After Update"
        assert audit.before_json["starting_balance"] == 0
        assert audit.after_json["starting_balance"] == 75


def test_tampered_active_classroom_cookie_does_not_grant_access(engine) -> None:
    _, owner_id = _logged_in_client(engine)
    other_client, _ = _logged_in_client(engine)

    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db,
            owner_user_id=owner_id,
            name="Cookie Cannot Authorize",
            school_year="2026-2027",
        )
        classroom_id = classroom.id

    other_client.cookies.set(
        settings.active_classroom_cookie_name,
        str(classroom_id),
    )

    dashboard = other_client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Cookie Cannot Authorize" not in dashboard.text
    assert "Active classroom" not in dashboard.text

    detail = other_client.get(f"/classrooms/{classroom_id}")
    assert detail.status_code == 404
