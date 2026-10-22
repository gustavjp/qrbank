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
from app.services.financial import (
    FinancialIntegrityError, post_reward_refund, post_transaction_reversal,
)
from app.services.reward_fulfillment import (
    FulfillmentConflictError, set_reward_fulfillment,
)


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


def _refund(
    client, classroom, enrollment, purchase_id, *, note="Mistaken purchase", request_id=None,
):
    client.get(f"/classrooms/{classroom}/students/{enrollment}")
    return client.post(
        f"/classrooms/{classroom}/students/{enrollment}/reward-purchases/{purchase_id}/refund",
        data={
            "note": note, "request_id": str(request_id or uuid.uuid4()),
            "csrf_token": _csrf(client),
        },
    )


def _latest_fulfillment_event_id(engine, purchase_id):
    with Session(engine) as db:
        return db.scalar(select(RewardFulfillmentEvent.id).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase_id,
        ).order_by(
            RewardFulfillmentEvent.occurred_at.desc(),
            RewardFulfillmentEvent.id.desc(),
        ).limit(1))


def _fulfill_student(
    client, classroom, enrollment, purchase_id, *,
    target_status="redeemed", expected_event_id, note="",
):
    client.get(f"/classrooms/{classroom}/students/{enrollment}")
    return client.post(
        f"/classrooms/{classroom}/students/{enrollment}/reward-purchases/{purchase_id}/fulfill",
        data={
            "target_status": target_status,
            "expected_event_id": str(expected_event_id),
            "note": note,
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


def test_refund_posts_exact_linked_reversal_cancels_purchase_and_restores_stock(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom, balance=100)
    reward = _reward(engine, classroom, stock=3, owner=owner)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]

    page = client.get(f"/classrooms/{classroom}/students/{student}")
    assert f"/reward-purchases/{purchase.id}/refund" in page.text
    response = _refund(client, classroom, student, purchase.id, note="Accidental purchase")
    assert response.status_code == 303 and "refunded=1" in response.headers["location"]

    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100
        assert db.get(Reward, reward).quantity_available == 3
        updated = db.get(RewardPurchase, purchase.id)
        assert updated.fulfillment_status == "cancelled"
        txs = list(db.scalars(select(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom,
        ).order_by(FinancialTransaction.display_number)))
        assert len(txs) == 2
        original, reversal = txs
        assert (original.amount, original.balance_before, original.balance_after) == (-30, 100, 70)
        assert (reversal.amount, reversal.balance_before, reversal.balance_after) == (30, 70, 100)
        assert reversal.transaction_type == "reversal"
        assert reversal.reverses_transaction_id == original.id
        assert reversal.reward_id == reward
        assert reversal.reward_code_snapshot == "R01"
        assert reversal.reward_name_snapshot == "Choose Your Seat"
        assert reversal.reward_price_snapshot == 30
        assert reversal.note == "Reward refund: Accidental purchase"
        assert original.transaction_type == "reward_purchase"
        assert updated.purchase_transaction_id == original.id
        fulfillment = list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase.id,
        ).order_by(RewardFulfillmentEvent.occurred_at, RewardFulfillmentEvent.id)))
        assert len(fulfillment) == 2
        assert {(e.from_status, e.to_status) for e in fulfillment} == {
            (None, "unused"), ("unused", "cancelled"),
        }
        assert next(e for e in fulfillment if e.to_status == "cancelled").note == "Accidental purchase"
        stock_events = list(db.scalars(select(RewardInventoryEvent).where(
            RewardInventoryEvent.classroom_id == classroom,
        )))
        assert len(stock_events) == 3
        restore = next(e for e in stock_events if e.event_type == "refund_restore")
        assert (restore.quantity_before, restore.delta, restore.quantity_after) == (2, 1, 3)
        assert restore.reward_purchase_id == purchase.id
        assert restore.financial_transaction_id == reversal.id

    history = client.get(f"/classrooms/{classroom}/students/{student}")
    assert "Reward refunded" not in history.text  # A normal history view has no success banner.
    assert "30 points · Cancelled" in history.text
    assert "Refund 30 points" not in history.text
    assert f"/transactions/{reversal.id}/reverse" not in history.text


def test_untracked_reward_refund_does_not_invent_inventory(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    assert _refund(client, classroom, student, purchase.id).status_code == 303
    assert _rows(engine, RewardInventoryEvent, classroom) == []
    with Session(engine) as db:
        assert db.get(Reward, reward).quantity_available is None
        assert db.get(Enrollment, student).current_balance == 100


def test_refund_replays_once_and_rejects_changed_request_or_second_refund(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    request_id = uuid.uuid4()
    assert _refund(client, classroom, student, purchase.id, request_id=request_id).status_code == 303
    replay = _refund(client, classroom, student, purchase.id, request_id=request_id)
    assert replay.status_code == 303 and "replayed=1" in replay.headers["location"]
    assert _refund(client, classroom, student, purchase.id,
                   note="Different reason", request_id=request_id).status_code == 409
    another = _refund(client, classroom, student, purchase.id)
    assert another.status_code == 422 and "already" in another.text
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2
    assert len(_rows(engine, FinancialRequest, classroom)) == 3
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100


def test_concurrent_distinct_refunds_cannot_credit_the_same_purchase_twice(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=2, owner=owner)
    with Session(engine) as db, db.begin():
        result = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = result.purchase.id

    def refund(_):
        with Session(engine) as db, db.begin():
            return post_reward_refund(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                note="Teacher correction", request_id=uuid.uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(refund, [0, 1]))
    assert sorted(r.rejection_code or "refunded" for r in outcomes) == [
        "already_refunded", "refunded",
    ]
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 3
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100
        assert db.get(Reward, reward).quantity_available == 2


def test_refund_and_new_purchase_compete_safely_for_last_stock(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    first = _enrollment(engine, classroom, "01")
    second = _enrollment(engine, classroom, "02")
    reward = _reward(engine, classroom, stock=1, owner=owner)
    with Session(engine) as db, db.begin():
        bought = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=first, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = bought.purchase.id

    def refund():
        with Session(engine) as db, db.begin():
            return post_reward_refund(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=first, purchase_id=purchase_id,
                note="Restock after mistake", request_id=uuid.uuid4(),
            )

    def buy():
        with Session(engine) as db, db.begin():
            return post_reward_purchase(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=second, reward_id=reward,
                expected_price=30, request_id=uuid.uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        refund_future = pool.submit(refund)
        buy_future = pool.submit(buy)
        refund_outcome = refund_future.result()
        buy_outcome = buy_future.result()

    assert refund_outcome.transaction is not None
    assert buy_outcome.rejection_code in (None, "out_of_stock")
    with Session(engine) as db:
        assert db.get(Enrollment, first).current_balance == 100
        assert db.get(Enrollment, second).current_balance == (
            70 if buy_outcome.rejection_code is None else 100
        )
        assert db.get(Reward, reward).quantity_available == (
            0 if buy_outcome.rejection_code is None else 1
        )
    assert len(_rows(engine, RewardPurchase, classroom)) == (
        2 if buy_outcome.rejection_code is None else 1
    )


def test_refunds_block_redeemed_purchases_and_generic_undo_of_refund(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    first = _enrollment(engine, classroom, "01")
    second = _enrollment(engine, classroom, "02")
    reward = _reward(engine, classroom)
    assert _student_buy(client, classroom, first, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    with Session(engine) as db, db.begin():
        db.get(RewardPurchase, purchase.id).fulfillment_status = "redeemed"
    assert _refund(client, classroom, first, purchase.id).status_code == 422
    with Session(engine) as db, db.begin():
        db.get(RewardPurchase, purchase.id).fulfillment_status = "unused"
    assert _refund(client, classroom, first, purchase.id).status_code == 303
    reversal = next(t for t in _rows(engine, FinancialTransaction, classroom)
                    if t.transaction_type == "reversal")
    with Session(engine) as db, db.begin():
        undo = post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=first, target_transaction_id=reversal.id,
            note="Try to undo refund", request_id=uuid.uuid4(),
        )
        assert undo.rejection_code == "unsupported_transaction_type"
    with Session(engine) as db:
        assert db.get(Enrollment, first).current_balance == 100
        assert db.get(Enrollment, second).current_balance == 100
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2


def test_refund_does_not_trust_new_catalog_price_or_student_status(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom, active=True)
    reward = _reward(engine, classroom, price=30)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    with Session(engine) as db, db.begin():
        db.get(Reward, reward).price_points = 100
        db.get(Reward, reward).name = "Updated reward"
        db.get(Reward, reward).active = False
        db.get(Enrollment, student).active = False
    assert _refund(client, classroom, student, purchase.id).status_code == 303
    refund_tx = next(t for t in _rows(engine, FinancialTransaction, classroom)
                     if t.transaction_type == "reversal")
    assert refund_tx.amount == 30
    assert refund_tx.reward_name_snapshot == "Choose Your Seat"
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100


def test_refund_enforces_tenant_student_membership_and_csrf(engine):
    client, owner = _client(engine)
    outsider_client, outsider = _client(engine)
    assistant_client, assistant = _client(engine)
    a = _classroom(engine, owner)
    b = _classroom(engine, outsider)
    student = _enrollment(engine, a, "01")
    other_student = _enrollment(engine, a, "02")
    foreign_student = _enrollment(engine, b)
    reward = _reward(engine, a)
    assert _student_buy(client, a, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, a)[0]
    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))

    assert _refund(outsider_client, a, student, purchase.id).status_code == 404
    assert _refund(assistant_client, a, student, purchase.id).status_code == 403
    assert _refund(client, a, foreign_student, purchase.id).status_code == 404
    assert _refund(client, a, other_student, purchase.id).status_code == 404
    assert _refund(client, b, foreign_student, purchase.id).status_code == 404
    url = f"/classrooms/{a}/students/{student}/reward-purchases/{purchase.id}/refund"
    bad_csrf = client.post(url, data={
        "note": "Mistake", "request_id": str(uuid.uuid4()), "csrf_token": "invalid",
    })
    assert bad_csrf.status_code == 403
    assert TestClient(app, follow_redirects=False).post(url, data={
        "note": "Mistake", "request_id": str(uuid.uuid4()), "csrf_token": "invalid",
    }).status_code == 401
    assert _refund(client, a, student, purchase.id, note="  ").status_code == 400
    assert len(_rows(engine, FinancialRequest, a)) == 1
    assert len(_rows(engine, FinancialTransaction, a)) == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(RewardPurchase, purchase.id).fulfillment_status == "unused"


def test_refund_rollback_preserves_original_purchase_and_all_stock(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=2, owner=owner)
    with Session(engine) as db, db.begin():
        outcome = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = outcome.purchase.id
    refund_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        with Session(engine) as db, db.begin():
            result = post_reward_refund(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                note="Simulated refund crash", request_id=refund_id,
            )
            assert result.transaction is not None
            raise RuntimeError("abort before commit")

    assert len(_rows(engine, FinancialRequest, classroom)) == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 2
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(Reward, reward).quantity_available == 1
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "unused"
    with Session(engine) as db, db.begin():
        result = post_reward_refund(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, purchase_id=purchase_id,
            note="Simulated refund crash", request_id=refund_id,
        )
        assert result.transaction is not None
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 100
        assert db.get(Reward, reward).quantity_available == 2


def test_refund_rejects_incompatible_inventory_configuration_without_partial_credit(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=1, owner=owner)
    with Session(engine) as db, db.begin():
        result = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = result.purchase.id
    with Session(engine) as db, db.begin():
        record = db.get(Reward, reward)
        record.inventory_tracking = False
        record.quantity_available = None

    with pytest.raises(FinancialIntegrityError):
        with Session(engine) as db, db.begin():
            post_reward_refund(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                note="Cannot restore disabled stock", request_id=uuid.uuid4(),
            )
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "unused"
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1


def test_refund_rejected_if_refunded_balance_would_overflow_integer_range(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    with Session(engine) as db, db.begin():
        result = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = result.purchase.id
    with Session(engine) as db, db.begin():
        db.get(Enrollment, student).current_balance = 2**31 - 10
    with Session(engine) as db, db.begin():
        refund = post_reward_refund(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, purchase_id=purchase_id,
            note="Overflow guard", request_id=uuid.uuid4(),
        )
        assert refund.rejection_code == "balance_out_of_range"
    with Session(engine) as db:
        assert db.get(Enrollment, student).current_balance == 2**31 - 10
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "unused"
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1


def test_fulfillment_redemption_and_reopening_are_audited_without_money_or_stock(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=2, owner=owner)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    initial_event = _latest_fulfillment_event_id(engine, purchase.id)

    student_page = client.get(f"/classrooms/{classroom}/students/{student}")
    assert "Mark redeemed" in student_page.text
    assert f"/reward-purchases/{purchase.id}/fulfill" in student_page.text
    catalog = client.get(f"/classrooms/{classroom}/rewards")
    assert "/reward-purchases" in catalog.text
    overview = client.get(f"/classrooms/{classroom}/reward-purchases")
    assert overview.status_code == 200 and "Fulfillment audit history" in overview.text

    response = _fulfill_student(
        client, classroom, student, purchase.id,
        expected_event_id=initial_event, note="Reward handed to student",
    )
    assert response.status_code == 303 and "fulfilled=1" in response.headers["location"]
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase.id).fulfillment_status == "redeemed"
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(Reward, reward).quantity_available == 1
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 2

    filtered = client.get(f"/classrooms/{classroom}/reward-purchases?show=redeemed")
    assert filtered.status_code == 200
    assert "Choose Your Seat" in filtered.text
    assert "Reward handed to student" in filtered.text
    assert "Reopen reward" in filtered.text
    assert client.get(f"/classrooms/{classroom}/reward-purchases?show=unused").text.count(
        "No reward purchases with this status"
    ) == 1

    redeemed_event = _latest_fulfillment_event_id(engine, purchase.id)
    reopened = client.post(
        f"/classrooms/{classroom}/reward-purchases/{purchase.id}/fulfill",
        data={
            "enrollment_id": str(student), "target_status": "unused",
            "expected_event_id": str(redeemed_event),
            "note": "Mistaken redemption confirmation", "csrf_token": _csrf(client),
        },
    )
    assert reopened.status_code == 303 and "changed=1" in reopened.headers["location"]
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase.id).fulfillment_status == "unused"
        assert db.get(Enrollment, student).current_balance == 70
        assert db.get(Reward, reward).quantity_available == 1
        events = list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase.id,
        )))
        assert len(events) == 3
        assert {(e.from_status, e.to_status) for e in events} == {
            (None, "unused"), ("unused", "redeemed"), ("redeemed", "unused"),
        }
        assert all(e.actor_user_id == owner for e in events)
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1
    assert len(_rows(engine, RewardInventoryEvent, classroom)) == 2


def test_stale_redemption_form_cannot_reapply_after_a_later_reopen(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    with Session(engine) as db, db.begin():
        purchased = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = purchased.purchase.id
    original_event = _latest_fulfillment_event_id(engine, purchase_id)

    with Session(engine) as db, db.begin():
        set_reward_fulfillment(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, purchase_id=purchase_id,
            target_status="redeemed", expected_event_id=original_event,
        )
    with pytest.raises(FulfillmentConflictError):
        with Session(engine) as db, db.begin():
            set_reward_fulfillment(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                target_status="redeemed", expected_event_id=original_event,
            )
    redeem_event = _latest_fulfillment_event_id(engine, purchase_id)
    with Session(engine) as db, db.begin():
        set_reward_fulfillment(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, purchase_id=purchase_id,
            target_status="unused", expected_event_id=redeem_event,
            note="Correction",
        )
    with pytest.raises(FulfillmentConflictError):
        with Session(engine) as db, db.begin():
            set_reward_fulfillment(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                target_status="redeemed", expected_event_id=original_event,
            )
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "unused"
        assert db.get(Enrollment, student).current_balance == 70
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1


def test_fulfillment_rollback_discards_status_and_event_together(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    with Session(engine) as db, db.begin():
        outcome = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = outcome.purchase.id
    event_id = _latest_fulfillment_event_id(engine, purchase_id)
    with pytest.raises(RuntimeError):
        with Session(engine) as db, db.begin():
            set_reward_fulfillment(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                target_status="redeemed", expected_event_id=event_id,
            )
            raise RuntimeError("abort before commit")
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "unused"
        assert db.get(Enrollment, student).current_balance == 70
        assert len(list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase_id,
        )))) == 1
    with Session(engine) as db, db.begin():
        set_reward_fulfillment(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, purchase_id=purchase_id,
            target_status="redeemed", expected_event_id=event_id,
        )
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase_id).fulfillment_status == "redeemed"


def test_fulfillment_respects_classroom_student_roles_and_csrf(engine):
    client, owner = _client(engine)
    outsider_client, outsider = _client(engine)
    assistant_client, assistant = _client(engine)
    classroom = _classroom(engine, owner)
    other_classroom = _classroom(engine, outsider)
    student = _enrollment(engine, classroom)
    other_student = _enrollment(engine, classroom, "02")
    foreign_student = _enrollment(engine, other_classroom)
    reward = _reward(engine, classroom)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    initial_event = _latest_fulfillment_event_id(engine, purchase.id)
    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))

    history_url = f"/classrooms/{classroom}/reward-purchases"
    assert outsider_client.get(history_url).status_code == 404
    history = assistant_client.get(history_url)
    assert history.status_code == 200
    assert "Choose Your Seat" in history.text
    assert "Mark redeemed" not in history.text
    assert "Fulfillment audit history" in history.text

    assert _fulfill_student(
        outsider_client, classroom, student, purchase.id,
        expected_event_id=initial_event,
    ).status_code == 404
    assert _fulfill_student(
        assistant_client, classroom, student, purchase.id,
        expected_event_id=initial_event,
    ).status_code == 403
    assert _fulfill_student(
        client, classroom, other_student, purchase.id,
        expected_event_id=initial_event,
    ).status_code == 404
    assert _fulfill_student(
        client, classroom, foreign_student, purchase.id,
        expected_event_id=initial_event,
    ).status_code == 404

    url = f"{history_url}/{purchase.id}/fulfill"
    params = {
        "enrollment_id": str(student), "target_status": "redeemed",
        "expected_event_id": str(initial_event), "csrf_token": "invalid",
    }
    assert client.post(url, data=params).status_code == 403
    assert TestClient(app, follow_redirects=False).post(url, data=params).status_code == 401
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase.id).fulfillment_status == "unused"
    assert len(_rows(engine, FinancialTransaction, classroom)) == 1


def test_fulfillment_validation_stale_and_cancelled_states(engine):
    client, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom)
    assert _student_buy(client, classroom, student, reward).status_code == 303
    purchase = _rows(engine, RewardPurchase, classroom)[0]
    original_event = _latest_fulfillment_event_id(engine, purchase.id)

    assert _fulfill_student(
        client, classroom, student, purchase.id,
        target_status="unused", expected_event_id=original_event,
        note="Not a correction",
    ).status_code == 409
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        target_status="cancelled", expected_event_id=original_event,
    ).status_code == 400
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        expected_event_id=uuid.uuid4(),
    ).status_code == 409

    assert _fulfill_student(
        client, classroom, student, purchase.id,
        expected_event_id=original_event,
    ).status_code == 303
    redeemed_event = _latest_fulfillment_event_id(engine, purchase.id)
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        target_status="unused", expected_event_id=redeemed_event,
        note="  ",
    ).status_code == 400
    assert _refund(client, classroom, student, purchase.id).status_code == 422
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        target_status="unused", expected_event_id=redeemed_event,
        note="Record was marked redeemed by mistake",
    ).status_code == 303
    assert _refund(client, classroom, student, purchase.id).status_code == 303
    cancelled_event = _latest_fulfillment_event_id(engine, purchase.id)
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        expected_event_id=cancelled_event,
    ).status_code == 409
    assert _fulfill_student(
        client, classroom, student, purchase.id,
        target_status="unused", expected_event_id=cancelled_event,
        note="Attempt undo refund",
    ).status_code == 409
    with Session(engine) as db:
        assert db.get(RewardPurchase, purchase.id).fulfillment_status == "cancelled"
        assert db.get(Enrollment, student).current_balance == 100
        assert db.scalar(select(RewardFulfillmentEvent.id).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase.id,
        ).limit(1)) is not None
        assert len(list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase.id,
        )))) == 4
    assert len(_rows(engine, FinancialTransaction, classroom)) == 2


def test_refund_and_redemption_compete_without_double_status_or_money(engine):
    _, owner = _client(engine)
    classroom = _classroom(engine, owner)
    student = _enrollment(engine, classroom)
    reward = _reward(engine, classroom, stock=1, owner=owner)
    with Session(engine) as db, db.begin():
        outcome = post_reward_purchase(
            db, actor_user_id=owner, classroom_id=classroom,
            enrollment_id=student, reward_id=reward,
            expected_price=30, request_id=uuid.uuid4(),
        )
        purchase_id = outcome.purchase.id
    original_event = _latest_fulfillment_event_id(engine, purchase_id)

    def refund():
        with Session(engine) as db, db.begin():
            result = post_reward_refund(
                db, actor_user_id=owner, classroom_id=classroom,
                enrollment_id=student, purchase_id=purchase_id,
                note="Refund requested", request_id=uuid.uuid4(),
            )
            return result.rejection_code or "refunded"

    def redeem():
        try:
            with Session(engine) as db, db.begin():
                set_reward_fulfillment(
                    db, actor_user_id=owner, classroom_id=classroom,
                    enrollment_id=student, purchase_id=purchase_id,
                    target_status="redeemed", expected_event_id=original_event,
                )
            return "redeemed"
        except FulfillmentConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        refund_future = pool.submit(refund)
        redeem_future = pool.submit(redeem)
        refund_outcome, redeem_outcome = refund_future.result(), redeem_future.result()

    assert (refund_outcome, redeem_outcome) in {
        ("refunded", "conflict"),
        ("already_redeemed", "redeemed"),
    }
    with Session(engine) as db:
        purchase = db.get(RewardPurchase, purchase_id)
        enrollment = db.get(Enrollment, student)
        stock = db.get(Reward, reward).quantity_available
        events = list(db.scalars(select(RewardFulfillmentEvent).where(
            RewardFulfillmentEvent.reward_purchase_id == purchase_id,
        )))
    assert len(events) == 2
    if refund_outcome == "refunded":
        assert (purchase.fulfillment_status, enrollment.current_balance, stock) == (
            "cancelled", 100, 1,
        )
        assert len(_rows(engine, FinancialTransaction, classroom)) == 2
        assert len(_rows(engine, RewardInventoryEvent, classroom)) == 3
    else:
        assert (purchase.fulfillment_status, enrollment.current_balance, stock) == (
            "redeemed", 70, 0,
        )
        assert len(_rows(engine, FinancialTransaction, classroom)) == 1
        assert len(_rows(engine, RewardInventoryEvent, classroom)) == 2
