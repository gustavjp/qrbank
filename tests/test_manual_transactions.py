from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token,
    create_user_session,
    register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_manual_transaction
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"manual-{uuid.uuid4()}@example.test",
            password="long-manual-password",
        )
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, user_id, *, starting_balance=0, allow_negative=False):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=user_id,
            name=f"Manual {uuid.uuid4()}", school_year="2026-2027",
        )
        row = db.get(ClassroomSettings, classroom.id)
        row.starting_balance = starting_balance
        row.allow_negative_balance = allow_negative
        return classroom.id


def _student(engine, user_id, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        enrollment = create_student_enrollment(
            db, user_id=user_id, classroom_id=classroom_id,
            student_code=code, display_name=None, notes=None,
        )
        return enrollment.enrollment.id


def _ledger(db, enrollment_id):
    return list(db.scalars(select(FinancialTransaction).where(
        FinancialTransaction.enrollment_id == enrollment_id,
    ).order_by(FinancialTransaction.display_number)))


def _post(client, classroom_id, enrollment_id, *,
          direction="credit", points="5", category="general",
          note="Classroom activity", request_id=None, csrf=None):
    return client.post(
        f"/classrooms/{classroom_id}/students/{enrollment_id}/transactions/manual",
        data={
            "request_id": str(request_id or uuid.uuid4()),
            "direction": direction,
            "points": points,
            "category": category,
            "note": note,
            "csrf_token": csrf if csrf is not None else client.cookies.get(settings.csrf_cookie_name),
        },
    )


def test_manual_add_and_subtract_are_atomic_auditable_integer_ledger_entries(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    enrollment_id = _student(engine, owner, classroom_id)

    page = client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    assert page.status_code == 200
    assert "Manual transaction" in page.text

    added = _post(client, classroom_id, enrollment_id, points="12", note="Earned points")
    deducted = _post(
        client, classroom_id, enrollment_id, direction="debit",
        points="4", note="Classroom fee",
    )
    assert added.status_code == deducted.status_code == 303
    assert added.headers["location"].endswith("?posted=1")

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 8
        first, second = _ledger(db, enrollment_id)
        assert (first.amount, first.balance_before, first.balance_after) == (12, 0, 12)
        assert (second.amount, second.balance_before, second.balance_after) == (-4, 12, 8)
        assert first.transaction_type == second.transaction_type == "manual"
        assert first.source == second.source == "manual"
        assert first.manual_category == second.manual_category == "general"
        assert first.note == "Earned points"
        assert second.note == "Classroom fee"
        assert first.actor_user_id == second.actor_user_id == owner
        assert first.student_code_snapshot == second.student_code_snapshot == "01"
        assert first.financial_request_id != second.financial_request_id

    detail = client.get(added.headers["location"])
    assert detail.status_code == 200
    assert "Earned points" in detail.text
    assert "Classroom fee" in detail.text
    assert "Category: General" in detail.text


def test_duplicate_manual_submission_replays_without_reposting_and_conflicts_on_change(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    enrollment_id = _student(engine, owner, classroom_id)
    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    request_id = uuid.uuid4()

    first = _post(client, classroom_id, enrollment_id, request_id=request_id)
    retry = _post(client, classroom_id, enrollment_id, request_id=request_id)
    changed = _post(client, classroom_id, enrollment_id, request_id=request_id, points="6")
    assert first.status_code == retry.status_code == 303
    assert retry.headers["location"].endswith("?replayed=1")
    assert changed.status_code == 409

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 5
        assert len(_ledger(db, enrollment_id)) == 1
        rows = list(db.scalars(select(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
        )))
        assert len(rows) == 1
        assert rows[0].idempotency_key == str(request_id)
        assert rows[0].status == "completed"


def test_insufficient_manual_deduction_is_rejected_and_replayable(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=3)
    enrollment_id = _student(engine, owner, classroom_id)
    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")
    rejected_id = uuid.uuid4()

    rejected = _post(
        client, classroom_id, enrollment_id, direction="debit",
        points="4", request_id=rejected_id,
    )
    assert rejected.status_code == 422
    assert "Insufficient balance" in rejected.text

    credited = _post(client, classroom_id, enrollment_id, points="10")
    assert credited.status_code == 303
    retry_rejected = _post(
        client, classroom_id, enrollment_id, direction="debit",
        points="4", request_id=rejected_id,
    )
    assert retry_rejected.status_code == 422

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 13
        assert [tx.amount for tx in _ledger(db, enrollment_id)] == [3, 10]
        request = db.scalar(select(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
            FinancialRequest.idempotency_key == str(rejected_id),
        ))
        assert request.status == "rejected"
        assert request.rejection_code == "insufficient_balance"


def test_manual_form_rejects_invalid_numbers_category_and_note(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    enrollment_id = _student(engine, owner, classroom_id)
    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")

    bad = [
        {"points": "0"},
        {"points": "-1"},
        {"points": "1.5"},
        {"points": "2147483648"},
        {"category": "invented"},
        {"note": "  "},
        {"direction": "debit", "category": "behavior_positive"},
        {"direction": "credit", "category": "behavior_negative"},
    ]
    for fields in bad:
        response = _post(client, classroom_id, enrollment_id, **fields)
        assert response.status_code == 400, (fields, response.text)

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 0
        assert _ledger(db, enrollment_id) == []


def test_manual_behavior_categories_create_linked_behavior_events(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, allow_negative=True)
    enrollment_id = _student(engine, owner, classroom_id)
    client.get(f"/classrooms/{classroom_id}/students/{enrollment_id}")

    positive = _post(
        client, classroom_id, enrollment_id, points="6",
        category="behavior_positive", note="Helping a classmate",
    )
    negative = _post(
        client, classroom_id, enrollment_id, direction="debit", points="8",
        category="behavior_negative", note="Classroom rule",
    )
    assert positive.status_code == negative.status_code == 303

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == -2
        txs = _ledger(db, enrollment_id)
        assert [tx.manual_category for tx in txs] == [
            "behavior_positive", "behavior_negative",
        ]
        events = list(db.scalars(select(BehaviorEvent).where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.enrollment_id == enrollment_id,
        )))
        assert len(events) == 2
        assert {event.behavior_type for event in events} == {"positive", "negative"}
        assert {event.financial_transaction_id for event in events} == {tx.id for tx in txs}


def test_manual_write_requires_membership_role_active_enrollment_and_csrf(engine):
    client, owner = _actor(engine)
    outsider_client, outsider = _actor(engine)
    classroom_a = _classroom(engine, owner)
    classroom_b = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_a)
    student_b = _student(engine, owner, classroom_b)
    client.get(f"/classrooms/{classroom_a}/students/{student_a}")
    outsider_client.get("/dashboard")

    assert _post(client, classroom_a, student_b).status_code == 404
    assert _post(outsider_client, classroom_a, student_a).status_code == 404

    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_a, user_id=outsider,
            role="assistant", created_by_user_id=owner,
        ))
    assert _post(outsider_client, classroom_a, student_a).status_code == 403
    assert _post(client, classroom_a, student_a, csrf="tampered").status_code == 403

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_a).active = False
    assert _post(client, classroom_a, student_a).status_code == 404

    with Session(engine) as db:
        assert db.get(Enrollment, student_a).current_balance == 0
        assert db.get(Enrollment, student_b).current_balance == 0
        assert _ledger(db, student_a) == []
        assert _ledger(db, student_b) == []


def test_concurrent_manual_debits_cannot_overdraw(engine):
    _, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    enrollment_id = _student(engine, owner, classroom_id)
    barrier = Barrier(2)

    def debit():
        with Session(engine) as db, db.begin():
            barrier.wait(timeout=10)
            return post_manual_transaction(
                db, actor_user_id=owner,
                classroom_id=classroom_id, enrollment_id=enrollment_id,
                direction="debit", points=5, category="general",
                note="Concurrent deduction", request_id=uuid.uuid4(),
            ).rejection_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(debit) for _ in range(2)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert sorted(str(code) for code in outcomes) == ["None", "insufficient_balance"]

    with Session(engine) as db:
        assert db.get(Enrollment, enrollment_id).current_balance == 0
        assert [tx.amount for tx in _ledger(db, enrollment_id)] == [5, -5]
