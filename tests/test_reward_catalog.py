from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.reward import Reward
from app.models.reward_purchase import RewardInventoryEvent, RewardPurchase
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)
from app.services.financial import post_reward_purchase, post_reward_refund
from app.services.reward_inventory import (
    RewardInventoryConflictError, adjust_reward_inventory, enable_reward_inventory,
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


def _inventory_events(engine, reward_id: uuid.UUID) -> list[RewardInventoryEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(RewardInventoryEvent).where(
            RewardInventoryEvent.reward_id == reward_id,
        ).order_by(RewardInventoryEvent.occurred_at, RewardInventoryEvent.id)))


def _enable_stock(
    client: TestClient, classroom_id: uuid.UUID, reward_id: uuid.UUID,
    quantity: int = 0, note: str = "Initial count",
):
    client.get(f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory")
    return client.post(
        f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory/enable",
        data={"initial_quantity": str(quantity), "note": note, "csrf_token": _csrf(client)},
    )


def _adjust_stock(
    client: TestClient, classroom_id: uuid.UUID, reward_id: uuid.UUID,
    *, expected: int, new: int, note: str = "Physical recount",
):
    client.get(f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory")
    return client.post(
        f"/classrooms/{classroom_id}/rewards/{reward_id}/inventory/adjust",
        data={
            "expected_quantity": str(expected), "new_quantity": str(new),
            "note": note, "csrf_token": _csrf(client),
        },
    )


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


def test_enable_zero_stock_and_adjust_with_full_audit(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    page = client.get(f"/classrooms/{classroom}/rewards/{reward_id}/inventory")
    assert page.status_code == 200 and "Enable inventory tracking" in page.text
    assert "Manage inventory" in client.get(f"/classrooms/{classroom}/rewards").text

    enabled = _enable_stock(client, classroom, reward_id, quantity=0)
    assert enabled.status_code == 303 and "changed=1" in enabled.headers["location"]
    reward = _reward(engine, classroom)
    assert reward.inventory_tracking is True and reward.quantity_available == 0
    assert not _inventory_events(engine, reward_id)  # Zero-delta events are forbidden by schema.

    assert _adjust_stock(
        client, classroom, reward_id, expected=0, new=5, note="Restocked supplies",
    ).status_code == 303
    assert _adjust_stock(
        client, classroom, reward_id, expected=5, new=2, note="Removed damaged items",
    ).status_code == 303
    reward = _reward(engine, classroom)
    assert reward.quantity_available == 2
    movements = _inventory_events(engine, reward_id)
    assert [(e.event_type, e.delta, e.quantity_before, e.quantity_after) for e in movements] == [
        ("manual_adjustment", 5, 0, 5),
        ("manual_adjustment", -3, 5, 2),
    ]
    assert [e.note for e in movements] == ["Restocked supplies", "Removed damaged items"]
    assert all(e.actor_user_id == owner for e in movements)
    audit = _audit_events(engine, reward_id)
    assert [e.event_type for e in audit] == [
        "reward_created", "reward_inventory_enabled",
        "reward_inventory_adjusted", "reward_inventory_adjusted",
    ]
    assert audit[1].before_json == {
        "inventory_tracking": False, "quantity_available": None,
    }
    assert audit[-1].after_json == {
        "inventory_tracking": True, "quantity_available": 2,
        "reason": "Removed damaged items",
    }
    history = client.get(f"/classrooms/{classroom}/rewards/{reward_id}/inventory")
    assert "Removed damaged items" in history.text and "Restocked supplies" in history.text
    assert "Available stock: 2" in history.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom,
        )) == 0


def test_inventory_initial_stock_validation_and_stale_forms(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    url = f"/classrooms/{classroom}/rewards/{reward_id}/inventory"
    assert _adjust_stock(client, classroom, reward_id, expected=0, new=3).status_code == 409
    assert _enable_stock(client, classroom, reward_id, quantity=-1).status_code == 400
    assert _enable_stock(client, classroom, reward_id, quantity=2**31).status_code == 400
    assert _enable_stock(client, classroom, reward_id, quantity=3, note=" ").status_code == 400
    assert _enable_stock(client, classroom, reward_id, quantity=3).status_code == 303
    assert _enable_stock(client, classroom, reward_id, quantity=3).status_code == 409
    initial = _inventory_events(engine, reward_id)
    assert len(initial) == 1
    assert (initial[0].event_type, initial[0].delta, initial[0].quantity_after) == (
        "initial_stock", 3, 3,
    )
    assert _adjust_stock(client, classroom, reward_id, expected=3, new=4).status_code == 303
    assert _adjust_stock(client, classroom, reward_id, expected=3, new=5).status_code == 409
    assert _adjust_stock(client, classroom, reward_id, expected=4, new=4).status_code == 409
    assert _adjust_stock(client, classroom, reward_id, expected=4, new=-1).status_code == 400
    assert _adjust_stock(client, classroom, reward_id, expected=4, new=2**31).status_code == 400
    assert _adjust_stock(client, classroom, reward_id, expected=4, new=3, note=" ").status_code == 400
    assert _adjust_stock(client, classroom, reward_id, expected=4, new=3, note="x" * 501).status_code == 400
    assert client.post(url + "/adjust", data={
        "expected_quantity": "4", "new_quantity": "1", "note": "Bad CSRF",
        "csrf_token": "invalid",
    }).status_code == 403
    with Session(engine) as db:
        assert db.get(Reward, reward_id).quantity_available == 4
    assert len(_inventory_events(engine, reward_id)) == 2


def test_reward_inventory_permissions_and_tenant_isolation(engine):
    owner_client, owner = _client(engine)
    teacher_client, teacher = _client(engine)
    assistant_client, assistant = _client(engine)
    other_client, other_owner = _client(engine)
    classroom = _classroom(engine, owner)
    foreign_classroom = _classroom(engine, other_owner)
    assert _create(owner_client, classroom).status_code == 303
    assert _create(other_client, foreign_classroom, code="R02").status_code == 303
    reward_id = _reward(engine, classroom).id
    foreign_reward = _reward(engine, foreign_classroom, "R02").id
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom, user_id=teacher,
                role="teacher", created_by_user_id=owner,
            ),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom, user_id=assistant,
                role="assistant", created_by_user_id=owner,
            ),
        ])

    path = f"/classrooms/{classroom}/rewards/{reward_id}/inventory"
    assert _enable_stock(teacher_client, classroom, reward_id, quantity=2).status_code == 303
    assistant_page = assistant_client.get(path)
    assert assistant_page.status_code == 200
    assert "Available stock: 2" in assistant_page.text
    assert "Save stock count" not in assistant_page.text
    assert _adjust_stock(assistant_client, classroom, reward_id, expected=2, new=3).status_code == 403
    assert _adjust_stock(other_client, classroom, reward_id, expected=2, new=3).status_code == 404
    assert other_client.get(path).status_code == 404
    assert owner_client.get(
        f"/classrooms/{classroom}/rewards/{foreign_reward}/inventory"
    ).status_code == 404
    assert _enable_stock(owner_client, classroom, foreign_reward, quantity=1).status_code == 404
    assert TestClient(app, follow_redirects=False).post(path + "/adjust", data={
        "expected_quantity": "2", "new_quantity": "1",
        "note": "Unknown actor", "csrf_token": "invalid",
    }).status_code == 401
    assert _reward(engine, classroom).quantity_available == 2
    assert len(_inventory_events(engine, reward_id)) == 1


def test_inventory_update_and_purchase_use_same_locked_count(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    assert _enable_stock(client, classroom, reward_id, quantity=2).status_code == 303
    enrollment_id, student_id = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as db, db.begin():
        db.add(Student(id=student_id, display_name="Inventory Student"))
        db.flush()
        db.add(Enrollment(
            id=enrollment_id, classroom_id=classroom,
            student_id=student_id, student_code="01", current_balance=100,
            active=True,
        ))

    def purchase():
        with Session(engine) as db, db.begin():
            return post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=enrollment_id, reward_id=reward_id,
                expected_price=25, request_id=uuid.uuid4(),
            )

    def adjustment():
        try:
            with Session(engine) as db, db.begin():
                adjust_reward_inventory(
                    db, actor_user_id=owner, classroom_id=classroom,
                    reward_id=reward_id, expected_quantity=2,
                    new_quantity=1, note="Correct physical stock",
                )
            return "adjusted"
        except RewardInventoryConflictError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        purchase_future = pool.submit(purchase)
        adjust_future = pool.submit(adjustment)
        purchase_result, adjust_result = purchase_future.result(), adjust_future.result()
    assert purchase_result.rejection_code is None
    assert adjust_result in {"adjusted", "stale"}
    with Session(engine) as db:
        reward = db.get(Reward, reward_id)
        purchase_record = db.scalar(select(RewardPurchase).where(
            RewardPurchase.classroom_id == classroom,
        ))
        assert purchase_record is not None
        assert db.get(Enrollment, enrollment_id).current_balance == 75
        assert reward.quantity_available == (0 if adjust_result == "adjusted" else 1)

    with Session(engine) as db, db.begin():
        refund = post_reward_refund(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=enrollment_id, purchase_id=purchase_record.id,
            request_id=uuid.uuid4(), note="Refund for inventory integration",
        )
        assert refund.transaction is not None
    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 100
        assert db.get(Reward, reward_id).quantity_available == (
            1 if adjust_result == "adjusted" else 2
        )
    events = _inventory_events(engine, reward_id)
    assert len(events) == (4 if adjust_result == "adjusted" else 3)
    assert any(e.event_type == "purchase_decrement" for e in events)
    assert any(e.event_type == "refund_restore" for e in events)


def test_inventory_transaction_rollback_reverts_count_and_events(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    assert _create(client, classroom).status_code == 303
    reward_id = _reward(engine, classroom).id
    assert _enable_stock(client, classroom, reward_id, quantity=1).status_code == 303
    with pytest.raises(RuntimeError):
        with Session(engine) as db, db.begin():
            adjust_reward_inventory(
                db, actor_user_id=owner, classroom_id=classroom,
                reward_id=reward_id, expected_quantity=1,
                new_quantity=10, note="Interrupted edit",
            )
            raise RuntimeError("abort stock change")
    assert _reward(engine, classroom).quantity_available == 1
    assert len(_inventory_events(engine, reward_id)) == 1
    assert len(_audit_events(engine, reward_id)) == 2

