from __future__ import annotations

import html
import re
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action
from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import (
    post_manual_transaction, post_scanned_action, post_transaction_reversal,
)
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"behavior-report-{uuid.uuid4()}@example.test",
            password="long-behavior-report-password",
        )
        verify_email_token(
            db, token=create_email_verification_token(db, user_id=user.id).token,
        )
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner, *, starting_balance=10, timezone_name="America/New_York"):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"Behavior {uuid.uuid4()}", school_year="2026-2027",
        )
        settings = db.get(ClassroomSettings, classroom.id)
        settings.starting_balance = starting_balance
        settings.timezone = timezone_name
        return classroom.id


def _student(engine, owner, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=f"Student {code}", notes=None,
        )
        return record.enrollment.id


def _manual(db, owner, classroom_id, student_id, *,
            direction="credit", category="behavior_positive", points=1, note="Helping"):
    return post_manual_transaction(
        db, actor_user_id=owner, classroom_id=classroom_id,
        enrollment_id=student_id, direction=direction, points=points,
        category=category, note=note, request_id=uuid.uuid4(),
    )


def _event_ids(markup):
    return re.findall(r"Event ([0-9a-f-]{36})", markup)


def test_history_authorized_tenant_scoped_and_counts_only_behavior_events(engine):
    client, owner = _actor(engine)
    assistant_client, assistant = _actor(engine)
    outsider_client, _ = _actor(engine)
    class_a = _classroom(engine, owner)
    class_b = _classroom(engine, owner)
    student_a = _student(engine, owner, class_a, "01")
    student_b = _student(engine, owner, class_b, "02")
    with Session(engine) as db, db.begin():
        post_scanned_action(
            db, actor_user_id=owner, classroom_id=class_a,
            enrollment_id=student_a, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        _manual(
            db, owner, class_a, student_a, direction="debit",
            category="behavior_negative", points=2, note="Classroom rule",
        )
        _manual(
            db, owner, class_a, student_a, category="general",
            note="Not a behavior event",
        )
        post_scanned_action(
            db, actor_user_id=owner, classroom_id=class_b,
            enrollment_id=student_b, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=class_a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))
        # Changing the current code cannot rewrite the posted snapshot.
        db.get(Enrollment, student_a).student_code = "77"

    url = f"/classrooms/{class_a}/behavior"
    with Session(engine) as db:
        count_events = db.scalar(select(func.count()).select_from(BehaviorEvent))
        count_ledger = db.scalar(select(func.count()).select_from(FinancialTransaction))

    response = client.get(url)
    assert response.status_code == 200
    assert "Classroom behavior history" in response.text
    assert "<strong>2</strong> total" in response.text
    assert "<strong>1</strong> positive" in response.text
    assert "<strong>1</strong> negative" in response.text
    assert len(_event_ids(response.text)) == 2
    assert "Positive Behavior +5" in response.text
    assert "Classroom rule" in response.text
    assert "Not a behavior event" not in response.text
    assert "Student 01" in response.text
    assert "Student 02" not in response.text

    assert assistant_client.get(url).status_code == 200
    assert outsider_client.get(url).status_code == 404
    assert TestClient(app, follow_redirects=False).get(url).status_code == 303
    assert client.get(url + f"?enrollment_id={student_b}").status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BehaviorEvent)) == count_events
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == count_ledger


def test_behavior_type_student_filters_and_totals_ignore_pagination(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_id, "01")
    student_b = _student(engine, owner, classroom_id, "02")
    with Session(engine) as db, db.begin():
        _manual(db, owner, classroom_id, student_a, note="Student A positive")
        _manual(
            db, owner, classroom_id, student_a, direction="debit",
            category="behavior_negative", note="Student A negative",
        )
        _manual(db, owner, classroom_id, student_b, note="Student B positive")

    url = f"/classrooms/{classroom_id}/behavior"
    student = client.get(url + f"?enrollment_id={student_a}")
    assert student.status_code == 200
    assert "<strong>2</strong> total" in student.text
    assert "Student A positive" in student.text
    assert "Student A negative" in student.text
    assert "Student B positive" not in student.text

    positive = client.get(url + "?behavior_type=positive")
    assert positive.status_code == 200
    assert "<strong>2</strong> total" in positive.text
    assert "<strong>2</strong> positive" in positive.text
    assert "<strong>0</strong> negative" in positive.text
    assert "Student A negative" not in positive.text


def test_zero_point_behavior_and_existing_reason_snapshots_are_readable(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A05",
        ))
        action.amount = 0
        db.flush()
        result = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        assert result.transaction is None
        event = db.scalar(select(BehaviorEvent).where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.enrollment_id == student_id,
        ))
        event.reason_code_snapshot = "HELP"
        event.reason_name_snapshot = "Helping Others"
        event.note = "Observed kindness"

    response = client.get(f"/classrooms/{classroom_id}/behavior")
    assert response.status_code == 200
    assert "<strong>1</strong> positive" in response.text
    assert "No points posted for this event" in response.text
    assert "Helping Others" in response.text
    assert "Observed kindness" in response.text
    assert "Positive Behavior +5" in response.text


def test_behavior_counts_stay_after_financial_reversal(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        posted = _manual(db, owner, classroom_id, student_id)
        reversed_tx = post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id,
            target_transaction_id=posted.transaction.id,
            note="Correct award", request_id=uuid.uuid4(),
        ).transaction
        reverse_num = reversed_tx.display_number

    response = client.get(f"/classrooms/{classroom_id}/behavior")
    assert response.status_code == 200
    assert "<strong>1</strong> total" in response.text
    assert f"Points reversed by TX{reverse_num:06d}" in response.text
    assert "Helping" in response.text


def test_behavior_local_date_range_crosses_dst_without_leaking_days(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    times = [
        datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc),
    ]
    with Session(engine) as db, db.begin():
        for timestamp in times:
            _manual(db, owner, classroom_id, student_id)
            db.flush()
            # Select by transaction link to update the specific new event.
            tx = db.scalar(
                select(FinancialTransaction).where(
                    FinancialTransaction.classroom_id == classroom_id,
                    FinancialTransaction.enrollment_id == student_id,
                    FinancialTransaction.transaction_type == "manual",
                ).order_by(FinancialTransaction.display_number.desc())
            )
            event = db.scalar(select(BehaviorEvent).where(
                BehaviorEvent.financial_transaction_id == tx.id,
            ))
            event.occurred_at = timestamp

    response = client.get(
        f"/classrooms/{classroom_id}/behavior?date_from=2026-03-08&date_to=2026-03-08",
    )
    assert response.status_code == 200
    assert "<strong>2</strong> total" in response.text
    assert len(_event_ids(response.text)) == 2
    assert "03:30 AM EDT" in response.text
    assert "11:30 PM EDT" in response.text
    assert "11:30 PM EST" not in response.text


def test_event_keyset_pagination_handles_identical_timestamps_and_preserves_filters(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        # CURRENT_TIMESTAMP is identical for these 54 rows in one transaction.
        for _ in range(54):
            _manual(db, owner, classroom_id, student_id)

    base = f"/classrooms/{classroom_id}/behavior"
    first = client.get(
        base + f"?enrollment_id={student_id}&behavior_type=positive",
    )
    assert first.status_code == 200
    first_ids = _event_ids(first.text)
    assert len(first_ids) == 50
    assert len(set(first_ids)) == 50
    assert "<strong>54</strong> total" in first.text
    next_match = re.search(r'href="([^"]*before=[^"]+)"', first.text)
    assert next_match
    next_url = html.unescape(next_match.group(1))
    assert f"enrollment_id={student_id}" in next_url
    assert "behavior_type=positive" in next_url

    older = client.get(next_url)
    assert older.status_code == 200
    older_ids = _event_ids(older.text)
    assert len(older_ids) == 4
    assert set(first_ids).isdisjoint(older_ids)
    assert "<strong>54</strong> total" in older.text
    assert "Older events" not in older.text


def test_invalid_behavior_filters_rejected(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    _student(engine, owner, classroom_id)
    base = f"/classrooms/{classroom_id}/behavior"
    for query in [
        "behavior_type=neutral",
        "date_from=2026-02-30",
        "date_from=03-08-2026",
        "date_from=2026-03-10&date_to=2026-03-08",
        "date_to=9999-12-31",
        "before=garbage",
        "before=2026-10-01T12%3A00%3A00%2B00%3A00%7Cnot-a-uuid",
    ]:
        response = client.get(base + "?" + query)
        assert response.status_code == 400, (query, response.text)
