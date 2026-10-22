from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.reward import Reward
from app.models.reward_purchase import (
    RewardFulfillmentEvent, RewardInventoryEvent, RewardPurchase,
)
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)
from app.services.financial import post_reward_purchase


PASSWORD = "reward-purchase-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"purchase-{uuid.uuid4()}@example.test", password=PASSWORD,
        )
        token = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=token.token)
        user_session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, user_session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner: uuid.UUID, *, allow_negative: bool = False) -> uuid.UUID:
    classroom_id = uuid.uuid4()
    with Session(engine) as db, db.begin():
        db.add(Classroom(
            id=classroom_id, name=f"Store {classroom_id}", school_year="2026-2027",
        ))
        db.flush()
        db.add_all([
            ClassroomSettings(
                classroom_id=classroom_id, allow_negative_balance=allow_negative,
            ),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom_id,
                user_id=owner, role="owner", created_by_user_id=owner,
            ),
        ])
    return classroom_id


def _enrollment(
    engine, classroom: uuid.UUID, code: str = "01", *,
    balance: int = 100, active: bool = True,
) -> uuid.UUID:
    enrollment_id, student_id = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as db, db.begin():
        db.add(Student(id=student_id, display_name=f"Student {code}"))
        db.flush()
        db.add(Enrollment(
            id=enrollment_id, student_id=student_id, classroom_id=classroom,
            student_code=code, current_balance=balance, active=active,
        ))
    return enrollment_id


def _reward(
    engine, classroom: uuid.UUID, *,
    code: str = "R01", price: int = 30, active: bool = True,
    stock: int | None = None, owner: uuid.UUID | None = None,
    from_time: datetime | None = None, until_time: datetime | None = None,
) -> uuid.UUID:
    reward_id = uuid.uuid4()
    with Session(engine) as db, db.begin():
        db.add(Reward(
            id=reward_id, classroom_id=classroom, reward_code=code,
            name="Choose Your Seat", reward_type="privilege", price_points=price,
            active=active, inventory_tracking=stock is not None,
            quantity_available=stock, available_from=from_time,
            available_until=until_time,
        ))
        db.flush()
        if stock is not None and stock > 0:
            db.add(RewardInventoryEvent(
                id=uuid.uuid4(), classroom_id=classroom,
                reward_id=reward_id, event_type="initial_stock",
                delta=stock, quantity_before=0, quantity_after=stock,
                actor_user_id=owner,
            ))
    return reward_id


def _rows(engine, model, classroom):
    with Session(engine) as db:
        return list(db.scalars(select(model).where(model.classroom_id == classroom)))


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _student_buy(
    client, classroom, enrollment, reward, *,
    expected_price=30, request_id=None,
):
    client.get(f"/classrooms/{classroom}/students/{enrollment}")
    return client.post(
        f"/classrooms/{classroom}/students/{enrollment}/rewards/purchase",
        data={
            "reward_id": str(reward), "expected_price": str(expected_price),
            "request_id": str(request_id or uuid.uuid4()),
            "csrf_token": _csrf(client),
        },
    )


def _store_buy(
    client, classroom, enrollment, reward, *,
    expected_price=30, request_id=None,
):
    client.get(f"/classrooms/{classroom}/rewards")
    return client.post(
        f"/classrooms/{classroom}/rewards/{reward}/purchase",
        data={
            "enrollment_id": str(enrollment), "expected_price": str(expected_price),
            "request_id": str(request_id or uuid.uuid4()),
            "csrf_token": _csrf(client),
        },
    )


def test_both_entrypoints_share_service_and_keep_immutable_snapshots(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)

    detail = client.get(f"/classrooms/{classroom}/students/{student}")
    store = client.get(f"/classrooms/{classroom}/rewards")
    assert detail.status_code == 200 and store.status_code == 200
    assert "Purchase reward" in detail.text
    assert f"/classrooms/{classroom}/rewards/{reward}/purchase" in store.text

    assert _student_buy(client, classroom, student, reward).status_code == 303
    with Session(engine) as db, db.begin():
        existing = db.get(Reward, reward)
        existing.reward_code = "R99"
        existing.name = "Renamed Seat"
        existing.price_points = 40

    assert _store_buy(
        client, classroom, student, reward, expected_price=40,
    ).status_code == 303
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 30
        txs = list(db.scalars(select(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom,
        ).order_by(FinancialTransaction.display_number)))
        purchases = list(db.scalars(select(RewardPurchase).where(
            RewardPurchase.classroom_id == classroom,
        ).order_by(RewardPurchase.purchased_at, RewardPurchase.id)))
        events = list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id.in_([p.id for p in purchases]),
        )))
        assert len(txs) == len(purchases) == len(events) == 2
        assert [tx.amount for tx in txs] == [-30, -40]
        assert [tx.balance_after for tx in txs] == [70, 30]
        assert txs[0].reward_code_snapshot == "R01"
        assert txs[0].reward_name_snapshot == "Choose Your Seat"
        assert txs[0].reward_price_snapshot == 30
        assert txs[1].reward_code_snapshot == "R99"
        assert all(p.fulfillment_status == "unused" for p in purchases)
        assert sorted(p.price_snapshot for p in purchases) == [30, 40]
        assert all(e.from_status is None and e.to_status == "unused" for e in events)
        assert all(tx.transaction_type == "reward_purchase" and tx.source == "reward_store"
                   and tx.actor_user_id == owner for tx in txs)
    history = client.get(f"/classrooms/{classroom}/students/{student}")
    assert "Reward: R01 — Choose Your Seat (30 points)" in history.text
    assert "Reward: R99 — Renamed Seat (40 points)" in history.text
    assert "Reward purchase history" in history.text
    assert "R01 — Choose Your Seat · 30 points · Unused" in history.text


def test_retries_do_not_double_charge_and_request_ids_are_input_bound(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    request_id = uuid.uuid4()
    assert _student_buy(
        client, classroom, student, reward, request_id=request_id,
    ).status_code == 303

    with Session(engine) as db, db.begin():
        db.get(Reward, reward).price_points = 50
        db.get(Reward, reward).active = False

    # Replays retrieve the original record even if the reward was edited.
    response = _store_buy(
        client, classroom, student, reward, request_id=request_id,
    )
    assert response.status_code == 303
    assert "replayed=1" in response.headers["location"]
    assert _student_buy(
        client, classroom, student, reward,
        request_id=request_id, expected_price=50,
    ).status_code == 409
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 70
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardPurchase, classroom)) == 1
    assert len(_rows(engine, FinancialRequest, classroom)) == 1


def test_insufficient_funds_even_when_negative_balances_are_allowed(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner, allow_negative=True)
    student = _enrollment(engine, classroom, balance=5)
    reward = _reward(engine, classroom)
    request_id = uuid.uuid4()
    response = _student_buy(
        client, classroom, student, reward, request_id=request_id,
    )
    assert response.status_code == 422
    assert "enough points" in response.text
    assert _student_buy(
        client, classroom, student, reward, request_id=request_id,
    ).status_code == 422
    requests = _rows(engine, FinancialRequest, classroom)
    assert len(requests) == 1
    assert requests[0].status == "rejected" and requests[0].rejection_code == "insufficient_balance"
    assert _rows(engine, FinancialTransaction, classroom) == []
    assert _rows(engine, RewardPurchase, classroom) == []
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 5


def test_inactive_student_and_zero_stock_cannot_purchase(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    inactive = _enrollment(engine, classroom, code="01", active=False)
    active = _enrollment(engine, classroom, code="02")
    reward = _reward(engine, classroom, stock=0, owner=owner)

    inactive_response = _store_buy(client, classroom, inactive, reward)
    assert inactive_response.status_code == 422
    assert "inactive" in inactive_response.text
    stock_response = _student_buy(client, classroom, active, reward)
    assert stock_response.status_code == 422
    assert "out of stock" in stock_response.text
    assert sorted(r.rejection_code for r in _rows(engine, FinancialRequest, classroom)) == [
        "inactive_student", "out_of_stock",
    ]
    assert not _rows(engine, FinancialTransaction, classroom)
    assert not _rows(engine, RewardPurchase, classroom)


def test_price_change_inactive_and_unavailable_reward_fail_without_charge(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, price=30)
    client.get(f"/classrooms/{classroom}/students/{student}")
    with Session(engine) as db, db.begin():
        db.get(Reward, reward).price_points = 40
    assert _student_buy(
        client, classroom, student, reward, expected_price=30,
    ).status_code == 422
    with Session(engine) as db, db.begin():
        db.get(Reward, reward).active = False
    assert _student_buy(
        client, classroom, student, reward, expected_price=40,
    ).status_code == 422
    with Session(engine) as db, db.begin():
        item = db.get(Reward, reward)
        item.active = True
        item.available_from = datetime.now(timezone.utc) + timedelta(days=1)
    assert _student_buy(
        client, classroom, student, reward, expected_price=40,
    ).status_code == 422
    with Session(engine) as db, db.begin():
        item = db.get(Reward, reward)
        item.available_from = None
        item.available_until = datetime.now(timezone.utc) - timedelta(days=1)
    assert _student_buy(
        client, classroom, student, reward, expected_price=40,
    ).status_code == 422
    assert len(_rows(engine, FinancialRequest, classroom)) == 4
    assert _rows(engine, FinancialTransaction, classroom) == []
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100


def test_stock_decrement_creates_auditable_event_and_prevents_selling_last_twice(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    first = _enrollment(engine, classroom, "01")
    second = _enrollment(engine, classroom, "02")
    reward = _reward(engine, classroom, price=30, stock=1, owner=owner)

    def purchase(enrollment_id: uuid.UUID):
        with Session(engine) as db, db.begin():
            return post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=enrollment_id, reward_id=reward,
                expected_price=30, request_id=uuid.uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(purchase, [first, second]))
    assert sorted(result.rejection_code or "paid" for result in outcomes) == ["out_of_stock", "paid"]
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardPurchase, classroom)) == 1
    events = _rows(engine, RewardInventoryEvent, classroom)
    assert len(events) == 2
    purchase_event = next(e for e in events if e.event_type == "purchase_decrement")
    assert (purchase_event.quantity_before, purchase_event.delta, purchase_event.quantity_after) == (1, -1, 0)
    with Session(engine) as db:
        assert db.get(Reward, reward).quantity_available == 0
        assert sorted([
            db.get(Enrollment, first).current_balance,
            db.get(Enrollment, second).current_balance,
        ]) == [70, 100]

    # Later requests cannot buy from empty tracked inventory.
    assert _student_buy(
        client, classroom, second, reward,
    ).status_code == 422


def test_concurrent_purchases_cannot_overdraw_a_single_student(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom, balance=100)
    first = _reward(engine, classroom, code="R01", price=60)
    second = _reward(engine, classroom, code="R02", price=60)

    def purchase(reward_id):
        with Session(engine) as db, db.begin():
            return post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, reward_id=reward_id,
                expected_price=60, request_id=uuid.uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(purchase, [first, second]))
    assert sorted(result.rejection_code or "paid" for result in outcomes) == ["insufficient_balance", "paid"]
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 40


def test_simultaneous_retries_with_same_request_id_charge_once(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=2, owner=owner)
    request_id = uuid.uuid4()

    def purchase(_):
        with Session(engine) as db, db.begin():
            return post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, reward_id=reward,
                expected_price=30, request_id=request_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(purchase, [0, 1]))
    assert sorted(result.replayed for result in outcomes) == [False, True]
    assert all(result.rejection_code is None for result in outcomes)
    assert len(_rows(engine, FinancialRequest, classroom)) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardPurchase, classroom)) == 1
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 2
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(Reward, reward).quantity_available == 1


def test_rollback_discards_balance_ledger_purchase_and_stock_together(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=2, owner=owner)
    request_id = uuid.uuid4()

    with pytest.raises(RuntimeError):
        with Session(engine) as db, db.begin():
            result = post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, reward_id=reward,
                expected_price=30, request_id=request_id,
            )
            assert result.transaction is not None
            raise RuntimeError("simulated crash before commit")

    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100
        assert db.get(Reward, reward).quantity_available == 2
    assert _rows(engine, FinancialRequest, classroom) == []
    assert _rows(engine, FinancialTransaction, classroom) == []
    assert _rows(engine, RewardPurchase, classroom) == []
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 1

    with Session(engine) as db, db.begin():
        outcome = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=request_id,
        )
        assert outcome.transaction is not None and outcome.replayed is False
    assert len(_rows(engine, FinancialRequest, classroom)) == 1
    assert len(_rows(engine, RewardPurchase, classroom)) == 1


def test_foreign_student_reward_and_memberships_cannot_cross_tenants(engine):
    owner_client, owner = _client(engine)
    outsider_client, outsider = _client(engine)
    assistant_client, assistant = _client(engine)
    a = _classroom(engine, owner)
    b = _classroom(engine, outsider)
    a_student = _enrollment(engine, a)
    b_student = _enrollment(engine, b)
    a_reward = _reward(engine, a)
    b_reward = _reward(engine, b)

    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))

    assert _student_buy(outsider_client, a, a_student, a_reward).status_code == 404
    assert _store_buy(assistant_client, a, a_student, a_reward).status_code == 403
    assert _student_buy(owner_client, a, b_student, a_reward).status_code == 404
    assert _store_buy(owner_client, a, a_student, b_reward).status_code == 404

    assert _rows(engine, FinancialTransaction, a) == []
    assert _rows(engine, FinancialTransaction, b) == []
    assert _rows(engine, RewardPurchase, a) == []
    with Session(engine) as db:
        assert db.get(Enrollment, a_student).current_balance == 100
        assert db.get(Enrollment, b_student).current_balance == 100


def test_anonymous_and_bad_csrf_or_form_inputs_cannot_purchase(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    url = f"/classrooms/{classroom}/students/{student}/rewards/purchase"
    raw = {
        "reward_id": str(reward), "expected_price": "30",
        "request_id": str(uuid.uuid4()),
    }
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.post(url, data={**raw, "csrf_token": "missing"}).status_code == 401
    assert client.post(url, data={**raw, "csrf_token": "wrong"}).status_code == 403
    client.get(f"/classrooms/{classroom}/students/{student}")
    assert client.post(url, data={
        **raw, "expected_price": "1.5", "csrf_token": _csrf(client),
    }).status_code == 422
    assert _rows(engine, FinancialRequest, classroom) == []
