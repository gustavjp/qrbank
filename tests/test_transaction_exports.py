from __future__ import annotations

import csv
import io
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
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_manual_transaction, post_transaction_reversal
from app.services.students import create_student_enrollment
from app.services.transaction_exports import CSV_COLUMNS


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"export-{uuid.uuid4()}@example.test",
            password="long-export-password",
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
            name=f"Export {uuid.uuid4()}", school_year="2026-2027",
        )
        settings_row = db.get(ClassroomSettings, classroom.id)
        settings_row.starting_balance = starting_balance
        settings_row.timezone = timezone_name
        return classroom.id


def _student(engine, owner, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        created = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=f"Student {code}", notes=None,
        )
        return created.enrollment.id


def _manual(db, owner, classroom_id, student_id, *,
            points=1, direction="credit", category="general", note="Test entry"):
    return post_manual_transaction(
        db, actor_user_id=owner, classroom_id=classroom_id,
        enrollment_id=student_id, direction=direction, points=points,
        category=category, note=note, request_id=uuid.uuid4(),
    ).transaction


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text, newline="")))


def test_csv_export_requires_membership_and_never_leaks_another_classroom(engine):
    client, owner = _actor(engine)
    assistant_client, assistant = _actor(engine)
    outsider_client, _ = _actor(engine)
    classroom_a = _classroom(engine, owner, starting_balance=7)
    classroom_b = _classroom(engine, owner, starting_balance=9)
    student_a = _student(engine, owner, classroom_a, "01")
    student_b = _student(engine, owner, classroom_b, "02")
    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))
    base = f"/classrooms/{classroom_a}/transactions/export.csv"
    with Session(engine) as db:
        tx_count = db.scalar(select(func.count()).select_from(FinancialTransaction))
        tx_a = db.scalar(select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_a,
        ))
        tx_b = db.scalar(select(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_b,
        ))

    response = client.get(base)
    rows = _rows(response)
    assert len(rows) == 1
    assert rows[0]["transaction_id"] == str(tx_a.id)
    assert rows[0]["student_code_snapshot"] == "01"
    assert rows[0]["amount"] == "7"
    assert rows[0]["balance_after"] == "7"
    assert rows[0]["transaction_type"] == "opening_balance"
    assert str(tx_b.id) not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment; filename=")

    assert _rows(assistant_client.get(base)) == rows
    assert outsider_client.get(base).status_code == 404
    assert client.get(base + f"?enrollment_id={student_b}").status_code == 404
    assert TestClient(app, follow_redirects=False).get(base).status_code == 303
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == tx_count


def test_csv_export_matches_filters_and_exceeds_html_page_without_truncating(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_id, "01")
    student_b = _student(engine, owner, classroom_id, "02")
    with Session(engine) as db, db.begin():
        for number in range(56):
            _manual(db, owner, classroom_id, student_a, points=number + 1)
        _manual(
            db, owner, classroom_id, student_b, category="behavior_positive",
            note="Other student's row",
        )

    base = f"/classrooms/{classroom_id}/transactions"
    query = (
        f"?enrollment_id={student_a}&transaction_type=manual"
        "&source=manual&category=general"
    )
    page = client.get(base + query)
    assert page.status_code == 200
    assert len(re.findall(r"<strong>TX[0-9]{6,}</strong>", page.text)) == 50
    assert "/transactions/export.csv?" in page.text
    assert "enrollment_id=" in page.text

    response = client.get(base + "/export.csv" + query)
    rows = _rows(response)
    assert len(rows) == 56
    assert set(row["enrollment_id"] for row in rows) == {str(student_a)}
    assert all(row["source"] == "manual" for row in rows)
    assert all(row["transaction_type"] == "manual" for row in rows)
    assert all(row["manual_category"] == "general" for row in rows)
    assert [int(row["display_number"]) for row in rows] == sorted(
        [int(row["display_number"]) for row in rows], reverse=True,
    )
    assert rows[0]["amount"] == "56"
    assert rows[-1]["amount"] == "1"
    assert list(rows[0]) == list(CSV_COLUMNS)

    # A filtered export with no entries has only the schema header.
    empty = client.get(base + "/export.csv?source=automatic")
    assert _rows(empty) == []
    assert empty.text.startswith('"transaction_id","display_number"')


def test_csv_export_uses_classroom_local_days_across_dst(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, timezone_name="America/New_York")
    student = _student(engine, owner, classroom_id)
    timestamps = [
        datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc),
    ]
    with Session(engine) as db, db.begin():
        for timestamp in timestamps:
            tx = _manual(db, owner, classroom_id, student)
            db.flush()
            tx.occurred_at = timestamp

    response = client.get(
        f"/classrooms/{classroom_id}/transactions/export.csv"
        "?date_from=2026-03-08&date_to=2026-03-08",
    )
    rows = _rows(response)
    assert len(rows) == 2
    assert all(row["classroom_timezone"] == "America/New_York" for row in rows)
    assert {row["occurred_at_classroom_local"] for row in rows} == {
        "2026-03-08T03:30:00-04:00", "2026-03-08T23:30:00-04:00",
    }
    assert {row["occurred_at_utc"] for row in rows} == {
        "2026-03-08T07:30:00+00:00", "2026-03-09T03:30:00+00:00",
    }


def test_csv_export_preserves_integer_amounts_reversal_links_and_escapes_formulas(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=20)
    student = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        dangerous = _manual(
            db, owner, classroom_id, student, points=3,
            direction="debit", note='=HYPERLINK("https://example.test","unsafe")',
        )
        db.flush()
        dangerous_id = dangerous.id
        dangerous.action_name_snapshot = " \t+SUM(1,2)"
        dangerous.quick_action_code_snapshot = "\r@ECHO"
        reverse = post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student, target_transaction_id=dangerous.id,
            note="Reversal", request_id=uuid.uuid4(),
        )
        reversal_id = reverse.transaction.id

    rows = _rows(client.get(f"/classrooms/{classroom_id}/transactions/export.csv"))
    by_id = {row["transaction_id"]: row for row in rows}
    assert by_id[str(dangerous_id)]["amount"] == "-3"
    assert by_id[str(dangerous_id)]["balance_before"] == "20"
    assert by_id[str(dangerous_id)]["balance_after"] == "17"
    assert by_id[str(dangerous_id)]["note"] == (
        """'=HYPERLINK("https://example.test","unsafe")"""
    )
    assert by_id[str(dangerous_id)]["action_name_snapshot"] == "' \t+SUM(1,2)"
    assert by_id[str(dangerous_id)]["quick_action_code_snapshot"] == "'\r@ECHO"
    assert by_id[str(reversal_id)]["reverses_transaction_id"] == str(dangerous_id)
    assert by_id[str(reversal_id)]["amount"] == "3"
    assert by_id[str(reversal_id)]["balance_after"] == "20"


def test_csv_export_rejects_invalid_filters_and_cross_classroom_ids(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    other_id = _classroom(engine, owner)
    _student(engine, owner, classroom_id)
    foreign_student = _student(engine, owner, other_id)
    base = f"/classrooms/{classroom_id}/transactions/export.csv"
    for query in [
        "transaction_type=unknown", "source=external", "category=unknown",
        "date_from=2026-02-30", "date_from=03-08-2026",
        "date_from=2026-03-09&date_to=2026-03-08",
        "date_to=9999-12-31",
    ]:
        response = client.get(base + "?" + query)
        assert response.status_code == 400, (query, response.text)
    assert client.get(base + f"?enrollment_id={foreign_student}").status_code == 404
