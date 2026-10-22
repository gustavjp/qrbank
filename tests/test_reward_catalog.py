from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.reward import Reward
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)


PASSWORD = "reward-catalog-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"reward-{uuid.uuid4()}@example.test", password=PASSWORD,
        )
        token = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=token.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = Classroom(
            id=uuid.uuid4(), name=f"Reward classroom {uuid.uuid4()}",
            school_year="2026-2027",
        )
        db.add(classroom)
        db.flush()
        db.add_all([
            ClassroomSettings(classroom_id=classroom.id),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom.id,
                user_id=owner_id, role="owner", created_by_user_id=owner_id,
            ),
        ])
        return classroom.id


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _create(
    client: TestClient, classroom_id: uuid.UUID, *,
    code: str = "R01", name: str = "Choose Your Seat",
    category: str = "privilege", price: int = 25,
    description: str = "Pick your seat for the day",
):
    client.get(f"/classrooms/{classroom_id}/rewards")
    return client.post(f"/classrooms/{classroom_id}/rewards/new", data={
        "code": code, "name": name, "reward_type": category,
        "price_points": str(price), "description": description,
        "csrf_token": _csrf(client),
    })


def _reward(engine, classroom_id: uuid.UUID, code: str = "R01") -> Reward:
    with Session(engine) as db:
        reward = db.scalar(select(Reward).where(
            Reward.classroom_id == classroom_id, Reward.reward_code == code,
        ))
        assert reward is not None
        db.expunge(reward)
        return reward


def _audit_events(engine, reward_id: uuid.UUID) -> list[AdminAuditEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.target_type == "reward",
            AdminAuditEvent.target_id == reward_id,
        ).order_by(AdminAuditEvent.occurred_at, AdminAuditEvent.id)))


def test_create_catalog_in_two_classes_and_audit_without_financial_entries(engine):
    client, owner = _client(engine)
    a = _classroom(engine, owner)
    b = _classroom(engine, owner)
    assert _create(client, a, code=" r01 ", price=25).status_code == 303
    assert _create(client, b, code="r01", price=75).status_code == 303

    reward_a, reward_b = _reward(engine, a), _reward(engine, b)
    assert reward_a.id != reward_b.id
    assert reward_a.reward_code == reward_b.reward_code == "R01"
    assert reward_a.price_points == 25 and reward_b.price_points == 75
    assert reward_a.reward_type == "privilege" and reward_a.active is True
    assert reward_a.inventory_tracking is False
    assert reward_a.quantity_available is None
    page = client.get(f"/classrooms/{a}/rewards")
    assert page.status_code == 200
    assert "R01" in page.text and "Choose Your Seat" in page.text
    assert "Create Reward" in page.text
    assert f"/classrooms/{a}/rewards" in client.get(f"/classrooms/{a}").text

    events = _audit_events(engine, reward_a.id)
    assert len(events) == 1
    assert events[0].event_type == "reward_created"
    assert events[0].before_json is None
    assert events[0].after_json["price_points"] == 25
    assert events[0].actor_user_id == owner
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id.in_([a, b])
        )) == 0
        assert db.scalar(select(func.count()).select_from(FinancialRequest).where(
            FinancialRequest.classroom_id.in_([a, b])
        )) == 0


def test_edit_toggle_and_noop_are_audited_without_balance_mutations(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    edit_url = f"/classrooms/{classroom}/rewards/{reward_id}/edit"
    assert client.get(edit_url).status_code == 200

    updated = {
        "code": "r15", "name": "Book Time", "reward_type": "other",
        "price_points": "40", "description": "Extra reading",
        "csrf_token": _csrf(client),
    }
    assert client.post(edit_url, data=updated).status_code == 303
    active_url = f"/classrooms/{classroom}/rewards/{reward_id}/active"
    assert client.post(active_url, data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(active_url, data={
        "active": "true", "csrf_token": _csrf(client),
    }).status_code == 303

    reward = _reward(engine, classroom, "R15")
    assert reward.price_points == 40 and reward.reward_type == "other"
    assert reward.name == "Book Time" and reward.active is True
    events = _audit_events(engine, reward_id)
    assert len(events) == 4
    assert {e.event_type for e in events} == {
        "reward_created", "reward_updated",
        "reward_deactivated", "reward_reactivated",
    }
    update = next(e for e in events if e.event_type == "reward_updated")
    assert update.before_json["price_points"] == 25
    assert update.after_json["price_points"] == 40

    assert client.post(edit_url, data=updated).status_code == 303
    assert client.post(active_url, data={
        "active": "true", "csrf_token": _csrf(client),
    }).status_code == 303
    assert len(_audit_events(engine, reward_id)) == 4
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom,
        )) == 0


def test_duplicate_codes_price_type_description_and_csrf_are_validated(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    assert _create(client, classroom, code="r01").status_code == 409
    assert _create(client, classroom, code="invalid code").status_code == 400
    assert _create(client, classroom, code="R02", name="  ").status_code == 400
    assert _create(client, classroom, code="R02", price=0).status_code == 400
    assert _create(client, classroom, code="R02", price=-1).status_code == 400
    assert _create(client, classroom, code="R02", price=2**31).status_code == 400
    assert _create(client, classroom, code="R02", category="money").status_code == 400
    assert _create(client, classroom, code="R02", description="x" * 2001).status_code == 400
    assert _create(client, classroom, code="R02", name="x" * 121).status_code == 400
    assert client.post(f"/classrooms/{classroom}/rewards/new", data={
        "code": "R02", "name": "Bad CSRF", "reward_type": "physical",
        "price_points": "5", "csrf_token": "invalid",
    }).status_code == 403
    assert client.post(f"/classrooms/{classroom}/rewards/new", data={
        "code": "R02", "name": "Fraction", "reward_type": "other",
        "price_points": "1.5", "csrf_token": _csrf(client),
    }).status_code == 422
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Reward).where(
            Reward.classroom_id == classroom,
        )) == 1
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom,
            AdminAuditEvent.target_type == "reward",
        )) == 1


def test_edit_validation_and_code_collision_preserve_existing_reward(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    assert _create(client, classroom, code="R02").status_code == 303
    reward_id = _reward(engine, classroom).id
    url = f"/classrooms/{classroom}/rewards/{reward_id}/edit"
    assert client.post(url, data={
        "code": "R02", "name": "Collision", "reward_type": "physical",
        "price_points": "100", "csrf_token": _csrf(client),
    }).status_code == 409
    assert client.post(url, data={
        "code": "R03", "name": "Bad Price", "reward_type": "privilege",
        "price_points": "-3", "csrf_token": _csrf(client),
    }).status_code == 400
    reward = _reward(engine, classroom)
    assert reward.name == "Choose Your Seat" and reward.price_points == 25
    assert len(_audit_events(engine, reward_id)) == 1


def test_unrelated_teacher_and_foreign_reward_ids_are_isolated(engine):
    first_client, first_owner = _client(engine)
    second_client, second_owner = _client(engine)
    a = _classroom(engine, first_owner)
    b = _classroom(engine, second_owner)
    assert _create(first_client, a).status_code == 303
    assert _create(second_client, b, code="R02").status_code == 303
    a_reward = _reward(engine, a).id
    b_reward = _reward(engine, b, "R02").id

    assert second_client.get(f"/classrooms/{a}/rewards").status_code == 404
    assert second_client.get(f"/classrooms/{a}/rewards/{a_reward}/edit").status_code == 404
    assert _create(second_client, a, code="R03").status_code == 404
    assert first_client.get(f"/classrooms/{a}/rewards/{b_reward}/edit").status_code == 404
    assert first_client.post(f"/classrooms/{a}/rewards/{b_reward}/edit", data={
        "code": "R99", "name": "Foreign", "reward_type": "other",
        "price_points": "1", "csrf_token": _csrf(first_client),
    }).status_code == 404
    assert first_client.post(f"/classrooms/{a}/rewards/{b_reward}/active", data={
        "active": "false", "csrf_token": _csrf(first_client),
    }).status_code == 404
    assert _reward(engine, b, "R02").price_points == 25


def test_teacher_can_manage_assistant_read_only_revoked_member_denied(engine):
    owner_client, owner = _client(engine)
    teacher_client, teacher = _client(engine)
    assistant_client, assistant = _client(engine)
    classroom = _classroom(engine, owner)
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom,
                user_id=teacher, role="teacher", created_by_user_id=owner,
            ),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom,
                user_id=assistant, role="assistant", created_by_user_id=owner,
            ),
        ])

    assert _create(teacher_client, classroom, code="R10").status_code == 303
    reward_id = _reward(engine, classroom, "R10").id
    assert assistant_client.get(f"/classrooms/{classroom}/rewards").status_code == 200
    assert "Create Reward" not in assistant_client.get(
        f"/classrooms/{classroom}/rewards"
    ).text
    assert assistant_client.get(
        f"/classrooms/{classroom}/rewards/{reward_id}/edit"
    ).status_code == 403
    assert _create(assistant_client, classroom, code="R11").status_code == 403
    assert assistant_client.post(f"/classrooms/{classroom}/rewards/{reward_id}/active", data={
        "active": "false", "csrf_token": _csrf(assistant_client),
    }).status_code == 403

    with Session(engine) as db, db.begin():
        member = db.scalar(select(ClassroomMembership).where(
            ClassroomMembership.classroom_id == classroom,
            ClassroomMembership.user_id == teacher,
            ClassroomMembership.ended_at.is_(None),
        ))
        assert member is not None
        member.ended_at = datetime.now(timezone.utc)

    assert teacher_client.get(f"/classrooms/{classroom}/rewards").status_code == 404
    assert teacher_client.post(f"/classrooms/{classroom}/rewards/{reward_id}/active", data={
        "active": "false", "csrf_token": _csrf(teacher_client),
    }).status_code == 404
    assert _reward(engine, classroom, "R10").active is True


def test_anonymous_reward_routes_require_login(engine):
    owner_client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(owner_client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    stranger = TestClient(app, follow_redirects=False)
    assert stranger.get(f"/classrooms/{classroom}/rewards").status_code == 401
    assert stranger.get(
        f"/classrooms/{classroom}/rewards/{reward_id}/edit"
    ).status_code == 401
    assert stranger.post(f"/classrooms/{classroom}/rewards/new", data={
        "code": "R99", "name": "No session", "reward_type": "other",
        "price_points": "1", "csrf_token": "missing",
    }).status_code == 401
