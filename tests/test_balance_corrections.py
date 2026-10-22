from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_balance_adjustment, post_transaction_reversal
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"correction-{uuid.uuid4()}@example.test",
            password="long-correction-password",
        )
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner, *, starting_balance=0, allow_negative=False):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"Corrections {uuid.uuid4()}", school_year="2026-2027",
        )
        settings_row = db.get(ClassroomSettings, classroom.id)
        settings_row.starting_balance = starting_balance
        settings_row.allow_negative_balance = allow_negative
        return classroom.id


def _student(engine, owner, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=None, notes=None,
        )
        return record.enrollment.id


def _ledger(db, enrollment_id):
    return list(db.scalars(
        select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == enrollment_id,
        ).order_by(FinancialTransaction.display_number)
    ))


def _csrf(client):
    return client.cookies.get(settings.csrf_cookie_name)


def _detail(client, classroom_id, enrollment_id):
    response = client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    assert response.status_code == 200
    return response


def _adjust(client, classroom_id, enrollment_id, *,
            target="10", note="Balance correction", request_id=None, csrf=None):
    return client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/balance-adjustment",
        data={
            "target_balance": target, "note": note,
            "request_id": str(request_id or uuid.uuid4()),
            "csrf_token": csrf if csrf is not None else _csrf(client),
        },
    )


def _reverse(client, classroom_id, enrollment_id, transaction_id, *,
             note="Correct the previous entry", request_id=None, csrf=None):
    return client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/transactions/{transaction_id}/reverse",
        data={
            "note": note, "request_id": str(request_id or uuid.uuid4()),
            "csrf_token": csrf if csrf is not None else _csrf(client),
        },
    )


def test_adjustments_post_locked_difference_and_snapshot_target(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    enrollment_id = _student(engine, owner, classroom_id)
    page = _detail(client, classroom_id, enrollment_id)
    assert "Set balance" in page.text

    up = _adjust(client, classroom_id, enrollment_id, target="11", note="Count correction")
    down = _adjust(client, classroom_id, enrollment_id, target="2", note="Second correction")
    assert up.status_code == down.status_code == 303

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 2
        opening, first, second = _ledger(db, enrollment_id)
        assert opening.amount == 5
        assert (first.amount, first.balance_before, first.balance_after) == (6, 5, 11)
        assert (second.amount, second.balance_before, second.balance_after) == (-9, 11, 2)
        assert first.adjustment_target_balance == 11
        assert second.adjustment_target_balance == 2
        assert first.note == "Count correction"
        assert second.note == "Second correction"
        assert first.actor_user_id == second.actor_user_id == owner
        assert first.transaction_type == second.transaction_type == "adjustment"
        assert first.source == second.source == "manual"

    detail = _detail(client, classroom_id, enrollment_id)
    assert "Set to: 11" in detail.text
    assert "Set to: 2" in detail.text


def test_noop_adjustment_has_durable_rejection_and_no_fake_ledger_entry(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=10)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)
    key = uuid.uuid4()

    first = _adjust(client, classroom_id, enrollment_id, target="10", request_id=key)
    retry = _adjust(client, classroom_id, enrollment_id, target="10", request_id=key)
    assert first.status_code == retry.status_code == 422
    assert "already at that amount" in first.text

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 10
        assert len(_ledger(db, enrollment_id)) == 1
        request = db.scalar(select(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
            FinancialRequest.idempotency_key == str(key),
        ))
        assert request.status == "rejected"
        assert request.rejection_code == "already_at_target"


def test_adjustment_idempotency_replay_and_changed_input_conflict(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)
    key = uuid.uuid4()

    first = _adjust(client, classroom_id, enrollment_id, target="9", request_id=key)
    replay = _adjust(client, classroom_id, enrollment_id, target="9", request_id=key)
    conflict = _adjust(client, classroom_id, enrollment_id, target="8", request_id=key)
    assert first.status_code == replay.status_code == 303
    assert replay.headers["location"].endswith("?replayed=1")
    assert conflict.status_code == 409

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 9
        assert len(_ledger(db, enrollment_id)) == 1


def test_adjustment_validates_integer_negative_balance_and_difference_overflow(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=2)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)

    for bad in ["1.5", "oops", "999999999999", "-2147483649", "2147483648"]:
        response = _adjust(client, classroom_id, enrollment_id, target=bad)
        assert response.status_code == 400, (bad, response.text)
    negative = _adjust(client, classroom_id, enrollment_id, target="-1")
    assert negative.status_code == 422
    assert "Negative balances are disabled" in negative.text

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 2
        assert [tx.amount for tx in _ledger(db, enrollment_id)] == [2]

    classroom_negative = _classroom(engine, owner, allow_negative=True)
    student_negative = _student(engine, owner, classroom_negative)
    _detail(client, classroom_negative, student_negative)
    minimum = _adjust(client, classroom_negative, student_negative, target="-2147483648")
    assert minimum.status_code == 303
    too_large = _adjust(client, classroom_negative, student_negative, target="2147483647")
    assert too_large.status_code == 422
    assert "integer transaction range" in too_large.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_negative).current_balance == -(2**31)
        assert [tx.amount for tx in _ledger(db, student_negative)] == [-(2**31)]


def test_reversal_chain_preserves_original_and_reversal_of_reversal(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=10)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)

    with Session(engine) as db, db.begin():
        adjustment = post_balance_adjustment(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=enrollment_id, target_balance=7,
            note="Before reversal", request_id=uuid.uuid4(),
        )
        first_target = adjustment.transaction.id

    reverse_key = uuid.uuid4()
    first = _reverse(client, classroom_id, enrollment_id, first_target,
                     request_id=reverse_key, note="Undo adjustment")
    assert first.status_code == 303

    with Session(engine) as db:
        opening, original, reversal = _ledger(db, enrollment_id)
        assert (opening.amount, original.amount, reversal.amount) == (10, -3, 3)
        assert reversal.reverses_transaction_id == original.id
        assert reversal.transaction_type == "reversal"
        assert reversal.source == "manual"
        assert reversal.note == "Undo adjustment"
        reversal_id = reversal.id
        assert db.get(Enrollment, enrollment_id).current_balance == 10

    second = _reverse(client, classroom_id, enrollment_id, reversal_id,
                      note="Restore original adjustment")
    assert second.status_code == 303
    retry_first = _reverse(client, classroom_id, enrollment_id, first_target,
                           request_id=reverse_key, note="Undo adjustment")
    another_first = _reverse(client, classroom_id, enrollment_id, first_target)
    assert retry_first.status_code == 303
    assert retry_first.headers["location"].endswith("?replayed=1")
    assert another_first.status_code == 422
    assert "already been directly reversed" in another_first.text

    with Session(engine) as db:
        history = _ledger(db, enrollment_id)
        assert [tx.amount for tx in history] == [10, -3, 3, -3]
        assert history[3].reverses_transaction_id == history[2].id
        assert history[1].amount == -3
        assert history[1].note == "Before reversal"
        assert db.get(Enrollment, enrollment_id).current_balance == 7

    detail = _detail(client, classroom_id, enrollment_id)
    assert "Reversed by TX" in detail.text
    assert "Reverses TX" in detail.text


def test_reversal_respects_negative_policy_and_does_not_modify_original(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)
    _adjust(client, classroom_id, enrollment_id, target="5")
    _adjust(client, classroom_id, enrollment_id, target="0")
    with Session(engine) as db:
        credit = _ledger(db, enrollment_id)[0]
        credit_id = credit.id

    rejected = _reverse(client, classroom_id, enrollment_id, credit_id)
    assert rejected.status_code == 422
    assert "Insufficient balance" in rejected.text
    with Session(engine) as db:
        assert len(_ledger(db, enrollment_id)) == 2
        assert db.get(Enrollment, enrollment_id).current_balance == 0

    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).allow_negative_balance = True
    allowed = _reverse(client, classroom_id, enrollment_id, credit_id)
    assert allowed.status_code == 303
    with Session(engine) as db:
        history = _ledger(db, enrollment_id)
        assert len(history) == 3
        assert history[0].amount == 5
        assert history[2].amount == -5
        assert history[2].reverses_transaction_id == history[0].id
        assert db.get(Enrollment, enrollment_id).current_balance == -5


def test_adjustment_and_reversal_require_tenant_role_csrf_and_active_adjustment(engine):
    client, owner = _actor(engine)
    outsider_client, outsider = _actor(engine)
    classroom_a = _classroom(engine, owner, starting_balance=5)
    classroom_b = _classroom(engine, owner, starting_balance=5)
    student_a = _student(engine, owner, classroom_a)
    student_b = _student(engine, owner, classroom_b)
    _detail(client, classroom_a, student_a)
    outsider_client.get("/dashboard")
    with Session(engine) as db:
        tx_a = _ledger(db, student_a)[0].id
        tx_b = _ledger(db, student_b)[0].id

    assert _adjust(client, classroom_a, student_b).status_code == 404
    assert _reverse(client, classroom_a, student_a, tx_b).status_code == 404
    assert _adjust(outsider_client, classroom_a, student_a).status_code == 404
    assert _reverse(outsider_client, classroom_a, student_a, tx_a).status_code == 404

    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_a, user_id=outsider,
            role="assistant", created_by_user_id=owner,
        ))
    assert _adjust(outsider_client, classroom_a, student_a).status_code == 403
    assert _reverse(outsider_client, classroom_a, student_a, tx_a).status_code == 403
    assert _adjust(client, classroom_a, student_a, csrf="bad").status_code == 403
    assert _reverse(client, classroom_a, student_a, tx_a, csrf="bad").status_code == 403

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_a).active = False
    assert _adjust(client, classroom_a, student_a).status_code == 404
    # Corrections of historical entries remain possible after deactivation.
    corrected = _reverse(client, classroom_a, student_a, tx_a)
    assert corrected.status_code == 303
    with Session(engine) as db:
        assert db.get(Enrollment, student_a).current_balance == 0
        assert db.get(Enrollment, student_b).current_balance == 5
        assert [tx.amount for tx in _ledger(db, student_a)] == [5, -5]


def test_concurrent_reversal_attempts_post_exactly_one_opposite_entry(engine):
    _, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=7)
    enrollment_id = _student(engine, owner, classroom_id)
    with Session(engine) as db:
        original = _ledger(db, enrollment_id)[0].id
    barrier = Barrier(2)

    def undo():
        with Session(engine) as db, db.begin():
            barrier.wait(timeout=10)
            result = post_transaction_reversal(
                db, actor_user_id=owner, classroom_id=classroom_id,
                enrollment_id=enrollment_id, target_transaction_id=original,
                note="Concurrent undo", request_id=uuid.uuid4(),
            )
            return result.rejection_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(undo) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(str(x) for x in results) == ["None", "already_reversed"]

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 0
        history = _ledger(db, enrollment_id)
        assert [tx.amount for tx in history] == [7, -7]
        assert history[1].reverses_transaction_id == original


def test_nonstandard_financial_sources_need_domain_specific_reversal(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=10)
    enrollment_id = _student(engine, owner, classroom_id)
    _detail(client, classroom_id, enrollment_id)

    # Model a future reward purchase without implementing purchase/inventory logic.
    with Session(engine) as db, db.begin():
        request_id = uuid.uuid4()
        db.add(FinancialRequest(
            id=request_id, classroom_id=classroom_id,
            idempotency_key=str(uuid.uuid4()), request_kind="reward_purchase",
            request_fingerprint=b"test", status="completed", actor_user_id=owner,
        ))
        db.flush()
        purchase = FinancialTransaction(
            id=uuid.uuid4(), classroom_id=classroom_id,
            enrollment_id=enrollment_id, financial_request_id=request_id,
            transaction_type="reward_purchase", source="reward_store",
            amount=-3, balance_before=10, balance_after=7,
            student_code_snapshot="01", actor_user_id=owner,
        )
        db.add(purchase)
        db.get(Enrollment, enrollment_id).current_balance = 7
        db.flush()
        purchase_id = purchase.id

    response = _reverse(client, classroom_id, enrollment_id, purchase_id)
    assert response.status_code == 422
    assert "domain-specific refund" in response.text
    detail = _detail(client, classroom_id, enrollment_id)
    assert "Reverse TX" in detail.text  # Ordinary entries remain reversible.

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 7
        assert [tx.amount for tx in _ledger(db, enrollment_id)] == [10, -3]
