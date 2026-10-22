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
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import (
    post_balance_adjustment, post_manual_transaction, post_scanned_action,
    post_transaction_reversal,
)
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"history-{uuid.uuid4()}@example.test",
            password="long-history-password",
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


def _classroom(engine, owner, *, starting_balance=0, timezone_name="America/New_York"):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"History {uuid.uuid4()}", school_year="2026-2027",
        )
        row = db.get(ClassroomSettings, classroom.id)
        row.starting_balance = starting_balance
        row.timezone = timezone_name
        return classroom.id


def _student(engine, owner, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=f"Student {code}", notes=None,
        )
        return record.enrollment.id


def _manual(db, owner, classroom_id, student_id, *, points=1, category="general"):
    return post_manual_transaction(
        db, actor_user_id=owner, classroom_id=classroom_id,
        enrollment_id=student_id, direction="credit", points=points,
        category=category, note="Test credit", request_id=uuid.uuid4(),
    ).transaction


def _tx_numbers(html_text):
    return [int(value) for value in re.findall(r"<strong>TX([0-9]{6,})</strong>", html_text)]


def _result_section(html_text):
    return html_text.split('<h2>Posted transactions', 1)[1]


def test_history_is_read_only_membership_authorized_and_classroom_scoped(engine):
    client, owner = _actor(engine)
    outsider_client, outsider = _actor(engine)
    classroom_a = _classroom(engine, owner, starting_balance=5)
    classroom_b = _classroom(engine, owner, starting_balance=9)
    student_a = _student(engine, owner, classroom_a)
    student_b = _student(engine, owner, classroom_b)
    with Session(engine) as db:
        tx_a = db.scalar(select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_a,
        ))
        tx_b = db.scalar(select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_b,
        ))
        before_count = db.scalar(select(func.count()).select_from(FinancialTransaction))

    response = client.get(f"/classrooms/{classroom_a}/transactions")
    assert response.status_code == 200
    assert "Classroom transaction history" in response.text
    assert f"TX{tx_a.display_number:06d}" in response.text
    assert f"TX{tx_b.display_number:06d}" not in response.text
    assert f"/classrooms/{classroom_a}/students/{student_a}" in response.text

    assert outsider_client.get(f"/classrooms/{classroom_a}/transactions").status_code == 404
    assert client.get(
        f"/classrooms/{classroom_a}/transactions?enrollment_id={student_b}",
    ).status_code == 404

    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_a, user_id=outsider,
            role="assistant", created_by_user_id=owner,
        ))
    assistant = outsider_client.get(f"/classrooms/{classroom_a}/transactions")
    assert assistant.status_code == 200
    assert f"TX{tx_a.display_number:06d}" in assistant.text
    assert f"TX{tx_b.display_number:06d}" not in assistant.text

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == before_count


def test_filter_by_student_type_source_category_and_snapshot_after_rename(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=4)
    student_a = _student(engine, owner, classroom_id, "01")
    student_b = _student(engine, owner, classroom_id, "02")
    with Session(engine) as db, db.begin():
        post_manual_transaction(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_a, direction="debit", points=2,
            category="behavior_negative", note="Rule deduction",
            request_id=uuid.uuid4(),
        )
        post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_a, payload="QUICK:QA1",
            request_id=uuid.uuid4(),
        )
        post_balance_adjustment(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_a, target_balance=12,
            note="Count correction", request_id=uuid.uuid4(),
        )
        # Code updates must not rewrite historical snapshot labels.
        db.get(Enrollment, student_a).student_code = "77"
    base = f"/classrooms/{classroom_id}/transactions"

    filtered = client.get(base + f"?enrollment_id={student_a}")
    assert filtered.status_code == 200
    assert "Student 01" in filtered.text
    assert "Rule deduction" in filtered.text
    assert "General +5" in filtered.text
    assert "Count correction" in filtered.text
    assert "Student 02" not in _result_section(filtered.text)

    manual = client.get(base + "?transaction_type=manual&category=behavior_negative")
    assert manual.status_code == 200
    assert len(_tx_numbers(manual.text)) == 1
    assert "Rule deduction" in manual.text
    assert "Count correction" not in manual.text

    scanner = client.get(base + "?source=scanner")
    assert scanner.status_code == 200
    assert len(_tx_numbers(scanner.text)) == 1
    assert "QA1" in scanner.text
    assert "General +5" in scanner.text
    assert "Rule deduction" not in scanner.text

    second = client.get(base + f"?enrollment_id={student_b}")
    assert second.status_code == 200
    assert len(_tx_numbers(second.text)) == 1
    assert "Student 02" in _result_section(second.text)
    assert "Rule deduction" not in second.text


def test_classroom_local_date_range_uses_midnight_boundaries_across_dst(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, timezone_name="America/New_York")
    student_id = _student(engine, owner, classroom_id)
    timestamps = [
        datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc),  # Mar 7 11:30 PM EST
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),  # Mar 8 3:30 AM EDT
        datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc),  # Mar 8 11:30 PM EDT
        datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc),  # Mar 9 12:30 AM EDT
    ]
    with Session(engine) as db, db.begin():
        for timestamp in timestamps:
            tx = _manual(db, owner, classroom_id, student_id)
            db.flush()
            tx.occurred_at = timestamp

    response = client.get(
        f"/classrooms/{classroom_id}/transactions?date_from=2026-03-08&date_to=2026-03-08",
    )
    assert response.status_code == 200
    assert len(_tx_numbers(response.text)) == 2
    assert "03:30 AM EDT" in response.text
    assert "11:30 PM EDT" in response.text
    assert "11:30 PM EST" not in response.text


def test_keyset_pagination_is_bounded_stable_and_preserves_filters(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        for _ in range(54):
            _manual(db, owner, classroom_id, student_id)
    first = client.get(
        f"/classrooms/{classroom_id}/transactions?transaction_type=manual&source=manual",
    )
    assert first.status_code == 200
    first_numbers = _tx_numbers(first.text)
    assert len(first_numbers) == 50
    assert first_numbers == sorted(first_numbers, reverse=True)

    match = re.search(r'href="([^"]*before=[0-9]+)"', first.text)
    assert match
    next_url = html.unescape(match.group(1))
    assert "transaction_type=manual" in next_url
    assert "source=manual" in next_url

    older = client.get(next_url)
    assert older.status_code == 200
    older_numbers = _tx_numbers(older.text)
    assert len(older_numbers) == 4
    assert set(first_numbers).isdisjoint(older_numbers)
    assert min(first_numbers) > max(older_numbers)
    assert "Older transactions" not in older.text


def test_reversal_links_resolve_even_when_target_on_another_page(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db:
        original = db.scalar(select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_id,
        ))
        original_id, original_number = original.id, original.display_number
    with Session(engine) as db, db.begin():
        for _ in range(51):
            _manual(db, owner, classroom_id, student_id)
        reversal = post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, target_transaction_id=original_id,
            note="Reverse opening", request_id=uuid.uuid4(),
        )
        reversal_number = reversal.transaction.display_number

    first = client.get(f"/classrooms/{classroom_id}/transactions")
    assert first.status_code == 200
    assert f"TX{reversal_number:06d}" in first.text
    assert f"Reverses TX{original_number:06d}" in first.text
    assert f'<strong>TX{original_number:06d}</strong>' not in first.text

    match = re.search(r'href="([^"]*before=[0-9]+)"', first.text)
    older = client.get(html.unescape(match.group(1)))
    assert older.status_code == 200
    assert f'<strong>TX{original_number:06d}</strong>' in older.text
    assert f"Reversed by TX{reversal_number:06d}" in older.text


def test_invalid_history_filters_rejected_without_cross_classroom_reads(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    _student(engine, owner, classroom_id)
    base = f"/classrooms/{classroom_id}/transactions"
    bad_queries = [
        "transaction_type=invented",
        "source=external",
        "category=not_real",
        "date_from=03-08-2026",
        "date_from=2026-02-30",
        "date_from=2026-03-10&date_to=2026-03-01",
        "before=-9",
    ]
    for params in bad_queries:
        response = client.get(base + "?" + params)
        assert response.status_code == 400, (params, response.text)
