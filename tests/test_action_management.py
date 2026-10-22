from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action, QuickAction
from app.models.audit import AdminAuditEvent
from app.models.classroom import ClassroomMembership
from app.models.financial import FinancialTransaction
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner


PASSWORD = "action-management-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"action-{uuid.uuid4()}@example.test", password=PASSWORD)
        verify = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verify.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id, name=f"Action class {uuid.uuid4()}",
            school_year="2026-2027",
        )
        return classroom.id


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _new_action(client: TestClient, classroom_id: uuid.UUID, *, code: str = "a01", amount: int = 5):
    client.get(f"/classrooms/{classroom_id}/actions")
    return client.post(f"/classrooms/{classroom_id}/actions/new", data={
        "code": code, "name": "General five", "kind": "general",
        "amount": amount, "description": "Teacher action", "csrf_token": _csrf(client),
    })


def test_action_and_quick_action_creation_is_classroom_scoped_and_audited(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    other_classroom = _classroom(engine, owner)

    assert _new_action(client, classroom_id, code=" a01 ").status_code == 303
    assert _new_action(client, other_classroom, code="a01", amount=10).status_code == 303

    with Session(engine) as db:
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A01"
        ))
        assert action is not None
        assert action.amount == 5
        action_id = action.id

    response = client.post(f"/classrooms/{classroom_id}/actions/quick/new", data={
        "code": " qa1 ", "action_id": str(action_id),
        "display_order": "1", "csrf_token": _csrf(client),
    })
    assert response.status_code == 303

    with Session(engine) as db:
        quick = db.scalar(select(QuickAction).where(
            QuickAction.classroom_id == classroom_id, QuickAction.quick_action_code == "QA1"
        ))
        assert quick is not None
        assert quick.action_id == action_id
        assert quick.display_order == 1
        events = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.event_type.in_(["action_created", "quick_action_created"]),
        )))
        assert len(events) == 2
        assert all(event.actor_user_id == owner for event in events)
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id
        )) == 0

    page = client.get(f"/classrooms/{classroom_id}/actions")
    assert page.status_code == 200
    assert "QA1" in page.text
    assert "A01" in page.text


def test_edit_and_toggle_preserve_ids_and_audit_before_after(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _new_action(client, classroom_id).status_code == 303
    assert _new_action(client, classroom_id, code="A02", amount=-5).status_code == 303
    with Session(engine) as db:
        actions = {action.action_code: action for action in db.scalars(
            select(Action).where(Action.classroom_id == classroom_id)
        )}
        first_id = actions["A01"].id
        second_id = actions["A02"].id

    created = client.post(f"/classrooms/{classroom_id}/actions/quick/new", data={
        "code": "QA1", "action_id": str(first_id), "display_order": "0",
        "csrf_token": _csrf(client),
    })
    assert created.status_code == 303
    with Session(engine) as db:
        quick_id = db.scalar(select(QuickAction.id).where(
            QuickAction.classroom_id == classroom_id, QuickAction.quick_action_code == "QA1"
        ))
        assert quick_id is not None

    assert client.post(f"/classrooms/{classroom_id}/actions/{first_id}/edit", data={
        "code": "A15", "name": "Updated", "kind": "behavior_positive",
        "amount": "20", "description": "Changed", "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/actions/quick/{quick_id}/edit", data={
        "code": "QA8", "action_id": str(second_id), "display_order": "8",
        "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/actions/{first_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(f"/classrooms/{classroom_id}/actions/quick/{quick_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303

    with Session(engine) as db:
        action = db.get(Action, first_id)
        quick = db.get(QuickAction, quick_id)
        assert action is not None and action.action_code == "A15" and not action.active
        assert quick is not None and quick.action_id == second_id and not quick.active
        updates = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.event_type.in_([
                "action_updated", "quick_action_updated",
                "action_deactivated", "quick_action_deactivated",
            ]),
        )))
        assert len(updates) == 4
        assert any(event.event_type == "action_updated" and
                   event.before_json["code"] == "A01" and
                   event.after_json["code"] == "A15" for event in updates)
        assert any(event.event_type == "quick_action_updated" and
                   event.before_json["action_id"] == str(first_id) and
                   event.after_json["action_id"] == str(second_id) for event in updates)


def test_duplicate_codes_and_invalid_action_values_rollback(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    assert _new_action(client, classroom_id).status_code == 303
    assert _new_action(client, classroom_id, code="a01").status_code == 409
    assert _new_action(client, classroom_id, code="bad code").status_code == 400
    assert _new_action(client, classroom_id, code="A09", amount=2**31).status_code == 400
    assert _new_action(client, classroom_id, code="A10", amount=-(2**31)).status_code == 400

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Action).where(
            Action.classroom_id == classroom_id
        )) == 1
        action_id = db.scalar(select(Action.id).where(Action.classroom_id == classroom_id))

    url = f"/classrooms/{classroom_id}/actions/quick/new"
    fields = {"code": "QA1", "action_id": str(action_id), "display_order": "1",
              "csrf_token": _csrf(client)}
    assert client.post(url, data=fields).status_code == 303
    assert client.post(url, data=fields).status_code == 409
    assert client.post(url, data={**fields, "code": "QA2", "display_order": "-1"}).status_code == 400
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(QuickAction).where(
            QuickAction.classroom_id == classroom_id
        )) == 1


def test_cross_tenant_guards_include_quick_action_mapping_and_mutations(engine) -> None:
    owner_client, owner = _client(engine)
    other_client, other = _client(engine)
    classroom_id = _classroom(engine, owner)
    other_classroom_id = _classroom(engine, other)
    assert _new_action(owner_client, classroom_id).status_code == 303
    assert _new_action(other_client, other_classroom_id, code="A02").status_code == 303
    with Session(engine) as db:
        owner_action_id = db.scalar(select(Action.id).where(Action.classroom_id == classroom_id))
        other_action_id = db.scalar(select(Action.id).where(Action.classroom_id == other_classroom_id))

    assert other_client.get(f"/classrooms/{classroom_id}/actions").status_code == 404
    assert other_client.get(f"/classrooms/{classroom_id}/actions/{owner_action_id}/edit").status_code == 404
    assert other_client.post(f"/classrooms/{classroom_id}/actions/{owner_action_id}/active", data={
        "active": "false", "csrf_token": _csrf(other_client),
    }).status_code == 404

    # Even an authorized owner cannot link an Action from a different classroom.
    spoofed = owner_client.post(f"/classrooms/{classroom_id}/actions/quick/new", data={
        "code": "QA1", "action_id": str(other_action_id),
        "display_order": "0", "csrf_token": _csrf(owner_client),
    })
    assert spoofed.status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(QuickAction).where(
            QuickAction.classroom_id == classroom_id
        )) == 0
        assert db.get(Action, owner_action_id).active is True


def test_teacher_can_manage_but_assistant_is_read_only_and_csrf_is_enforced(engine) -> None:
    owner_client, owner = _client(engine)
    teacher_client, teacher = _client(engine)
    assistant_client, assistant = _client(engine)
    classroom_id = _classroom(engine, owner)
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(id=uuid.uuid4(), classroom_id=classroom_id,
                                user_id=teacher, role="teacher", created_by_user_id=owner),
            ClassroomMembership(id=uuid.uuid4(), classroom_id=classroom_id,
                                user_id=assistant, role="assistant", created_by_user_id=owner),
        ])

    assert _new_action(teacher_client, classroom_id, code="T01").status_code == 303
    assert assistant_client.get(f"/classrooms/{classroom_id}/actions").status_code == 200
    assert "T01" in assistant_client.get(f"/classrooms/{classroom_id}/actions").text
    assert "Create Action" not in assistant_client.get(f"/classrooms/{classroom_id}/actions").text
    assert _new_action(assistant_client, classroom_id, code="X01").status_code == 403

    invalid_csrf = owner_client.post(f"/classrooms/{classroom_id}/actions/new", data={
        "code": "X02", "name": "CSRF", "kind": "general",
        "amount": "5", "csrf_token": "invalid",
    })
    assert invalid_csrf.status_code == 403
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Action).where(
            Action.classroom_id == classroom_id
        )) == 1
