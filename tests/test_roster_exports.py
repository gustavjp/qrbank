from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment, Student
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_manual_transaction
from app.services.roster_exports import ROSTER_CSV_COLUMNS, iter_classroom_roster_csv
from app.services.students import (
    create_student_enrollment, select_classroom_enrollments, set_enrollment_active,
)


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"roster-export-{uuid.uuid4()}@example.test",
            password="long-roster-export-password",
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


def _classroom(engine, owner, *, starting_balance=0, allow_negative=False):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"Roster {uuid.uuid4()}", school_year="2026-2027",
        )
        setting = db.get(ClassroomSettings, classroom.id)
        setting.starting_balance = starting_balance
        setting.allow_negative_balance = allow_negative
        return classroom.id


def _student(engine, owner, classroom_id, code="01", name="Alex", notes="Private note"):
    with Session(engine) as db, db.begin():
        created = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=name, notes=notes,
        )
        return created.enrollment.id


def _manual(db, owner, classroom_id, student_id, *, points=1):
    return post_manual_transaction(
        db, actor_user_id=owner, classroom_id=classroom_id,
        enrollment_id=student_id, direction="credit", points=points,
        category="general", note="Roster balance test", request_id=uuid.uuid4(),
    )


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text, newline="")))


def test_roster_csv_is_authorized_tenant_scoped_and_read_only(engine):
    owner_client, owner = _actor(engine)
    assistant_client, assistant = _actor(engine)
    outsider_client, _ = _actor(engine)
    class_a = _classroom(engine, owner, starting_balance=6)
    class_b = _classroom(engine, owner, starting_balance=17)
    student_a = _student(engine, owner, class_a, code="01")
    student_b = _student(engine, owner, class_b, code="02")
    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=class_a, user_id=assistant,
            role="assistant", created_by_user_id=owner,
        ))

    url = f"/classrooms/{class_a}/students/export.csv"
    with Session(engine) as db:
        before_transactions = db.scalar(select(func.count()).select_from(FinancialTransaction))
        before_balances = list(db.execute(
            select(Enrollment.id, Enrollment.current_balance).order_by(Enrollment.id)
        ))

    response = owner_client.get(url)
    rows = _rows(response)
    assert len(rows) == 1
    assert rows[0]["enrollment_id"] == str(student_a)
    assert rows[0]["classroom_id"] == str(class_a)
    assert rows[0]["student_code"] == "01"
    assert rows[0]["current_balance"] == "6"
    assert rows[0]["active"] == "true"
    assert str(student_b) not in response.text
    assert "Private note" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    assert _rows(assistant_client.get(url)) == rows
    assert outsider_client.get(url).status_code == 404
    assert TestClient(app, follow_redirects=False).get(url).status_code == 303

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == before_transactions
        assert list(db.execute(
            select(Enrollment.id, Enrollment.current_balance).order_by(Enrollment.id)
        )) == before_balances


def test_roster_csv_all_and_active_filter_current_posted_balance(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=10)
    active_id = _student(engine, owner, classroom_id, code="01", name="Ada")
    inactive_id = _student(engine, owner, classroom_id, code="02", name="Bea")
    _student(engine, owner, classroom_id, code="03", name=None)
    with Session(engine) as db, db.begin():
        _manual(db, owner, classroom_id, active_id, points=7)
        set_enrollment_active(
            db, user_id=owner, classroom_id=classroom_id,
            enrollment_id=inactive_id, active=False,
        )

    base = f"/classrooms/{classroom_id}/students"
    active_page = client.get(base + "?show=active")
    assert active_page.status_code == 200
    assert 'students/export.csv?show=active' in active_page.text
    assert 'students/export.csv?show=all' in client.get(base + "?show=all").text

    all_rows = _rows(client.get(base + "/export.csv"))
    active_rows = _rows(client.get(base + "/export.csv?show=active"))
    assert len(all_rows) == 3
    assert len(active_rows) == 2
    assert [r["student_code"] for r in all_rows] == ["01", "02", "03"]
    assert list(all_rows[0]) == list(ROSTER_CSV_COLUMNS)
    by_id = {r["enrollment_id"]: r for r in all_rows}
    assert by_id[str(active_id)]["current_balance"] == "17"
    assert by_id[str(inactive_id)]["current_balance"] == "10"
    assert by_id[str(inactive_id)]["active"] == "false"
    assert by_id[str(inactive_id)]["ended_on"]
    assert by_id[str(active_id)]["ended_on"] == ""
    assert by_id[str(active_id)]["started_on"]
    assert set(r["enrollment_id"] for r in active_rows) == {
        r["enrollment_id"] for r in all_rows if r["active"] == "true"
    }


def test_roster_csv_preserves_negative_integer_and_escapes_spreadsheet_formulas(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=-4, allow_negative=True)
    name = '=HYPERLINK("https://example.test","click")\nX,Y'
    student_id = _student(
        engine, owner, classroom_id, code="-CODE",
        name=name, notes="SECRET TEACHER NOTE",
    )
    rows = _rows(client.get(f"/classrooms/{classroom_id}/students/export.csv"))
    assert len(rows) == 1
    assert rows[0]["enrollment_id"] == str(student_id)
    assert rows[0]["student_code"] == "'-CODE"
    assert rows[0]["display_name"] == "'" + name
    assert rows[0]["current_balance"] == "-4"
    assert "SECRET TEACHER NOTE" not in str(rows)
    assert "notes" not in rows[0]


def test_roster_csv_empty_and_many_students_are_complete_and_stably_sorted(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    url = f"/classrooms/{classroom_id}/students/export.csv"
    blank = client.get(url)
    assert _rows(blank) == []
    assert blank.text.startswith('"classroom_id","enrollment_id"')

    with Session(engine) as db, db.begin():
        for number in range(65):
            create_student_enrollment(
                db, user_id=owner, classroom_id=classroom_id,
                student_code=f"{number:02d}", display_name=None, notes=None,
            )
    rows = _rows(client.get(url))
    assert len(rows) == 65
    assert [row["student_code"] for row in rows] == [
        f"{number:02d}" for number in range(65)
    ]
    assert all(row["current_balance"] == "0" for row in rows)
    assert all(row["active"] == "true" for row in rows)


def test_roster_snapshot_stays_consistent_during_concurrent_financial_post(engine):
    _, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    _student(engine, owner, classroom_id, code="01")
    second_id = _student(engine, owner, classroom_id, code="02")

    with Session(engine) as read_db, read_db.begin():
        read_db.execute(text(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        ))
        selection = select_classroom_enrollments(
            read_db, user_id=owner, classroom_id=classroom_id,
        )
        iterator = iter_classroom_roster_csv(read_db, selection)
        header = next(iterator)
        first = next(iterator)

        with Session(engine) as write_db, write_db.begin():
            _manual(write_db, owner, classroom_id, second_id, points=12)

        csv_text = header + first + "".join(iterator)
        rows = list(csv.DictReader(io.StringIO(csv_text, newline="")))
        assert len(rows) == 2
        assert [row["current_balance"] for row in rows] == ["5", "5"]

    with Session(engine) as check_db:
        assert check_db.get(Enrollment, second_id).current_balance == 17


def test_roster_csv_denies_bad_filters_and_revoked_memberships(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    _student(engine, owner, classroom_id)
    url = f"/classrooms/{classroom_id}/students/export.csv"
    assert client.get(url + "?show=inactive").status_code == 400
    assert client.get(url + "?show=active").status_code == 200
    assert client.get(f"/classrooms/{uuid.uuid4()}/students/export.csv").status_code == 404

    with Session(engine) as db, db.begin():
        membership = db.scalar(select(ClassroomMembership).where(
            ClassroomMembership.classroom_id == classroom_id,
            ClassroomMembership.user_id == owner,
        ))
        membership.ended_at = datetime.now(timezone.utc)
    assert client.get(url).status_code == 404
