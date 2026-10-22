from __future__ import annotations

import csv
import html
import io
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
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.behavior_exports import BEHAVIOR_CSV_COLUMNS
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import (
    post_manual_transaction, post_scanned_action, post_transaction_reversal,
)
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"behavior-export-{uuid.uuid4()}@example.test",
            password="long-behavior-export-password",
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
            name=f"Behavior export {uuid.uuid4()}", school_year="2026-2027",
        )
        classroom_settings = db.get(ClassroomSettings, classroom.id)
        classroom_settings.starting_balance = starting_balance
        classroom_settings.timezone = timezone_name
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


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text, newline="")))
    if rows:
        assert list(rows[0]) == list(BEHAVIOR_CSV_COLUMNS)
    return rows


def test_behavior_export_membership_tenant_isolation_and_read_only(engine):
    client, owner = _actor(engine)
    assistant_client, assistant = _actor(engine)
    outsider_client, _ = _actor(engine)
    class_a = _classroom(engine, owner)
    class_b = _classroom(engine, owner)
    student_a = _student(engine, owner, class_a, "01")
    student_b = _student(engine, owner, class_b, "02")
    with Session(engine) as db, db.begin():
        _manual(db, owner, class_a, student_a, note="A positive")
        _manual(
            db, owner, class_a, student_a, direction="debit",
            category="behavior_negative", note="A negative",
        )
        _manual(db, owner, class_a, student_a, category="general", note="Not behavior")
        _manual(db, owner, class_b, student_b, note="Foreign behavior")
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=class_a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))
    with Session(engine) as db:
        event_count = db.scalar(select(func.count()).select_from(BehaviorEvent))
        ledger_count = db.scalar(select(func.count()).select_from(FinancialTransaction))
    url = f"/classrooms/{class_a}/behavior/export.csv"

    response = client.get(url)
    rows = _rows(response)
    assert len(rows) == 2
    assert {row["behavior_type"] for row in rows} == {"positive", "negative"}
    assert {row["enrollment_id"] for row in rows} == {str(student_a)}
    assert all(row["classroom_id"] == str(class_a) for row in rows)
    assert "Foreign behavior" not in response.text
    assert "Not behavior" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    assert _rows(assistant_client.get(url)) == rows
    assert outsider_client.get(url).status_code == 404
    assert client.get(url + f"?enrollment_id={student_b}").status_code == 404
    assert TestClient(app, follow_redirects=False).get(url).status_code == 303

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BehaviorEvent)) == event_count
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == ledger_count


def test_behavior_export_matches_page_filters_and_includes_every_page(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_id, "01")
    student_b = _student(engine, owner, classroom_id, "02")
    with Session(engine) as db, db.begin():
        for number in range(57):
            _manual(db, owner, classroom_id, student_a, note=f"Positive {number}")
        _manual(db, owner, classroom_id, student_a, direction="debit",
                category="behavior_negative", note="Negative")
        _manual(db, owner, classroom_id, student_b, note="Other student")
    base = f"/classrooms/{classroom_id}/behavior"
    params = f"?enrollment_id={student_a}&behavior_type=positive"
    page = client.get(base + params)
    assert page.status_code == 200
    assert len(re.findall(r"Event [0-9a-f-]{36}", page.text)) == 50
    assert "<strong>57</strong> total" in page.text
    assert html.escape(f"/classrooms/{classroom_id}/behavior/export.csv?") in page.text
    assert "behavior_type=positive" in page.text

    response = client.get(base + "/export.csv" + params)
    rows = _rows(response)
    assert len(rows) == 57
    assert {row["enrollment_id"] for row in rows} == {str(student_a)}
    assert {row["behavior_type"] for row in rows} == {"positive"}
    keys = [(row["occurred_at_utc"], row["event_id"]) for row in rows]
    assert keys == sorted(keys, reverse=True)
    assert len({row["event_id"] for row in rows}) == 57

    empty = client.get(base + "/export.csv?behavior_type=negative&enrollment_id=" + str(student_b))
    assert _rows(empty) == []
    assert empty.text.startswith('"event_id","classroom_id"')


def test_behavior_export_zero_point_reason_snapshots_reversals_and_escaping(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=20)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        posted = _manual(
            db, owner, classroom_id, student_id, direction="debit",
            category="behavior_negative", points=3,
            note='=HYPERLINK("https://example.test","unsafe")',
        )
        db.flush()
        reversal = post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, target_transaction_id=posted.transaction.id,
            note="Undo", request_id=uuid.uuid4(),
        ).transaction
        db.flush()
        first_event = db.scalar(select(BehaviorEvent).where(
            BehaviorEvent.financial_transaction_id == posted.transaction.id,
        ))
        first_event.note = "\t+SUM(1,2)"
        first_event.reason_code_snapshot = "@BAD"
        first_event.reason_name_snapshot = "  =HARMFUL()"
        reverse_id = reversal.id
        tx_id = posted.transaction.id
        tx_number = posted.transaction.display_number
        reverse_number = reversal.display_number

        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A05",
        ))
        action.amount = 0
        db.flush()
        scanned = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        assert scanned.transaction is None
        db.get(Enrollment, student_id).student_code = "77"
        student = db.scalar(select(Student).join(
            Enrollment, Enrollment.student_id == Student.id,
        ).where(Enrollment.id == student_id))
        student.display_name = "=Name"

    rows = _rows(client.get(f"/classrooms/{classroom_id}/behavior/export.csv"))
    assert len(rows) == 2
    by_tx = {row["financial_transaction_id"]: row for row in rows}
    financial = by_tx[str(tx_id)]
    assert financial["student_code"] == "01"  # immutable financial snapshot
    assert financial["student_name"] == "'=Name"
    assert financial["points_amount"] == "-3"
    assert financial["financial_display_number"] == str(tx_number)
    assert financial["financial_note"] == """'=HYPERLINK("https://example.test","unsafe")"""
    assert financial["event_note"] == "'\t+SUM(1,2)"
    assert financial["reason_code_snapshot"] == "'@BAD"
    assert financial["reason_name_snapshot"] == "'  =HARMFUL()"
    assert financial["reversed_by_transaction_id"] == str(reverse_id)
    assert financial["reversed_by_display_number"] == str(reverse_number)
    zero = by_tx[""]
    assert zero["behavior_type"] == "positive"
    assert zero["points_amount"] == ""
    assert zero["financial_display_number"] == ""
    assert zero["reversed_by_transaction_id"] == ""
    assert zero["action_name_snapshot"] == "Positive Behavior +5"


def test_behavior_export_filters_local_dates_across_dst(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, timezone_name="America/New_York")
    student_id = _student(engine, owner, classroom_id)
    timestamps = [
        datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc),
    ]
    with Session(engine) as db, db.begin():
        for timestamp in timestamps:
            posted = _manual(db, owner, classroom_id, student_id)
            db.flush()
            event = db.scalar(select(BehaviorEvent).where(
                BehaviorEvent.financial_transaction_id == posted.transaction.id,
            ))
            event.occurred_at = timestamp

    rows = _rows(client.get(
        f"/classrooms/{classroom_id}/behavior/export.csv"
        "?date_from=2026-03-08&date_to=2026-03-08",
    ))
    assert len(rows) == 2
    assert {row["occurred_at_utc"] for row in rows} == {
        "2026-03-08T07:30:00+00:00", "2026-03-09T03:30:00+00:00",
    }
    assert {row["occurred_at_classroom_local"] for row in rows} == {
        "2026-03-08T03:30:00-04:00", "2026-03-08T23:30:00-04:00",
    }
    assert {row["classroom_timezone"] for row in rows} == {"America/New_York"}


def test_behavior_export_rejects_bad_filters_and_foreign_students(engine):
    client, owner = _actor(engine)
    class_a = _classroom(engine, owner)
    class_b = _classroom(engine, owner)
    _student(engine, owner, class_a)
    foreign_student = _student(engine, owner, class_b)
    url = f"/classrooms/{class_a}/behavior/export.csv"
    for query in [
        "behavior_type=neutral",
        "date_from=2026-02-30",
        "date_from=03-08-2026",
        "date_from=2026-03-10&date_to=2026-03-08",
        "date_to=9999-12-31",
    ]:
        response = client.get(url + "?" + query)
        assert response.status_code == 400, (query, response.text)
    assert client.get(url + f"?enrollment_id={foreign_student}").status_code == 404
