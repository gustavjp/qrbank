from __future__ import annotations

import html
import re
import uuid
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action
from app.models.audit import AdminAuditEvent
from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.behavior_analytics import classroom_weekly_behavior_trend
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import (
    post_manual_transaction, post_scanned_action, post_transaction_reversal,
)
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"trend-{uuid.uuid4()}@example.test",
            password="long-trend-test-password",
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


def _classroom(engine, owner, *, timezone_name="America/New_York"):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"Trend {uuid.uuid4()}", school_year="2026-2027",
        )
        settings_row = db.get(ClassroomSettings, classroom.id)
        settings_row.timezone = timezone_name
        settings_row.starting_balance = 20
        return classroom.id


def _student(engine, owner, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        created = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code=code, display_name=f"Student {code}", notes=None,
        )
        return created.enrollment.id


def _manual(db, owner, classroom_id, student_id, *,
            positive=True, category=None, occurred_at=None):
    posted = post_manual_transaction(
        db, actor_user_id=owner, classroom_id=classroom_id,
        enrollment_id=student_id,
        direction="credit" if positive else "debit",
        points=1,
        category=category or ("behavior_positive" if positive else "behavior_negative"),
        note="Trend event", request_id=uuid.uuid4(),
    )
    db.flush()
    event = db.scalar(select(BehaviorEvent).where(
        BehaviorEvent.financial_transaction_id == posted.transaction.id,
    ))
    if event is not None and occurred_at is not None:
        event.occurred_at = occurred_at
    return event, posted.transaction


def _chart_rows(markup):
    matches = re.findall(
        r'data-week-start="(\d{4}-\d{2}-\d{2})" data-positive-count="(\d+)" data-negative-count="(\d+)"',
        markup,
    )
    return [(date.fromisoformat(day), int(positive), int(negative))
            for day, positive, negative in matches]


def test_weekly_aggregation_uses_local_mondays_across_dst(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    stamps = [
        (datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc), True),
        (datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), True),
        (datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc), False),
        (datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc), True),
    ]
    with Session(engine) as db, db.begin():
        for stamp, positive in stamps:
            _manual(db, owner, classroom_id, student, positive=positive, occurred_at=stamp)
    with Session(engine) as db:
        trend = classroom_weekly_behavior_trend(
            db, user_id=owner, classroom_id=classroom_id,
            today=date(2026, 3, 10),
        )
    assert len(trend.weeks) == 8
    assert trend.starts_on == date(2026, 1, 19)
    assert trend.ends_on == date(2026, 3, 15)
    assert [week.starts_on.weekday() for week in trend.weeks] == [0] * 8
    assert (trend.positive_count, trend.negative_count, trend.total_count) == (3, 1, 4)
    by_date = {week.starts_on: week for week in trend.weeks}
    assert (by_date[date(2026, 3, 2)].positive_count,
            by_date[date(2026, 3, 2)].negative_count) == (2, 1)
    assert (by_date[date(2026, 3, 9)].positive_count,
            by_date[date(2026, 3, 9)].negative_count) == (1, 0)
    assert sum(week.total_count for week in trend.weeks[:-2]) == 0

    # The browser report uses the same database aggregate and selected end date.
    page = client.get(f"/classrooms/{classroom_id}/behavior?date_to=2026-03-10")
    assert page.status_code == 200
    rows = _chart_rows(page.text)
    assert len(rows) == 8
    assert rows[-2:] == [
        (date(2026, 3, 2), 2, 1),
        (date(2026, 3, 9), 1, 0),
    ]
    assert "Classroom-local Monday" in page.text
    assert "4</strong> events in this chart window" in page.text


def test_trend_respects_student_type_and_date_filters_without_other_classrooms(engine):
    client, owner = _actor(engine)
    classroom_a = _classroom(engine, owner)
    classroom_b = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_a, "01")
    student_other = _student(engine, owner, classroom_a, "02")
    student_foreign = _student(engine, owner, classroom_b, "03")
    april = datetime(2026, 4, 15, 12, tzinfo=timezone.utc)
    may = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
    with Session(engine) as db, db.begin():
        _manual(db, owner, classroom_a, student_a, positive=True, occurred_at=april)
        _manual(db, owner, classroom_a, student_a, positive=False, occurred_at=may)
        _manual(db, owner, classroom_a, student_other, positive=True, occurred_at=may)
        _manual(db, owner, classroom_b, student_foreign, positive=True, occurred_at=may)
        _manual(db, owner, classroom_a, student_a, category="general", occurred_at=may)
    with Session(engine) as db:
        trend = classroom_weekly_behavior_trend(
            db, user_id=owner, classroom_id=classroom_a, enrollment_id=student_a,
            behavior_type="positive", date_from=date(2026, 4, 1),
            date_to=date(2026, 5, 15), today=date(2026, 10, 22),
        )
    assert (trend.positive_count, trend.negative_count) == (1, 0)
    assert trend.ends_on == date(2026, 5, 17)
    assert [w.starts_on for w in trend.weeks if w.total_count] == [date(2026, 4, 13)]

    url = (f"/classrooms/{classroom_a}/behavior"
           f"?enrollment_id={student_a}&behavior_type=positive"
           "&date_from=2026-04-01&date_to=2026-05-15")
    page = client.get(url)
    assert page.status_code == 200
    assert sum(p for _, p, _ in _chart_rows(page.text)) == 1
    assert sum(n for _, _, n in _chart_rows(page.text)) == 0
    assert "<strong>1</strong> total" in page.text


def test_weekly_counts_include_zero_point_and_reversed_behavior_events(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    at = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)
    with Session(engine) as db, db.begin():
        awarded_event, tx = _manual(
            db, owner, classroom_id, student, occurred_at=at,
        )
        post_transaction_reversal(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student, target_transaction_id=tx.id,
            note="Undo award", request_id=uuid.uuid4(),
        )
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A05",
        ))
        action.amount = 0
        db.flush()
        zero = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        assert zero.transaction is None
        db.flush()
        zero_event = db.scalar(select(BehaviorEvent).where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.enrollment_id == student,
            BehaviorEvent.financial_transaction_id.is_(None),
        ))
        zero_event.occurred_at = at
    with Session(engine) as db:
        original_balance = db.scalar(select(Enrollment.current_balance).where(
            Enrollment.id == student,
        ))
        original_tx_count = db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        ))
        trend = classroom_weekly_behavior_trend(
            db, user_id=owner, classroom_id=classroom_id,
            date_to=date(2026, 10, 22),
        )
    assert trend.total_count == 2
    assert trend.positive_count == 2
    assert trend.negative_count == 0

    response = client.get(
        f"/classrooms/{classroom_id}/behavior?date_to=2026-10-22",
    )
    assert response.status_code == 200
    assert sum(p for _, p, _ in _chart_rows(response.text)) == 2
    with Session(engine) as db:
        assert db.scalar(select(Enrollment.current_balance).where(
            Enrollment.id == student,
        )) == original_balance
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == original_tx_count


def test_chart_is_eight_weeks_while_summary_and_history_can_be_older(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        _manual(db, owner, classroom_id, student,
                occurred_at=datetime(2026, 2, 2, 14, tzinfo=timezone.utc))
        _manual(db, owner, classroom_id, student,
                occurred_at=datetime(2026, 8, 25, 14, tzinfo=timezone.utc))
    response = client.get(
        f"/classrooms/{classroom_id}/behavior?date_to=2026-08-26",
    )
    assert response.status_code == 200
    assert "<strong>2</strong> total" in response.text
    assert "<strong>1</strong> events in this chart window" in response.text
    weeks = _chart_rows(response.text)
    assert len(weeks) == 8
    assert weeks[-1] == (date(2026, 8, 24), 1, 0)
    assert sum(p + n for _, p, n in weeks) == 1
    assert "2026-07-06 through 2026-08-30" in response.text

    with Session(engine) as db:
        trend = classroom_weekly_behavior_trend(
            db, user_id=owner, classroom_id=classroom_id,
            date_to=date(2026, 8, 26),
        )
    assert trend.starts_on == date(2026, 7, 6)
    assert trend.ends_on == date(2026, 8, 30)


def test_chart_remains_constant_across_history_pagination(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        for _ in range(54):
            _manual(db, owner, classroom_id, student)
        db.flush()
        db.execute(update(BehaviorEvent).where(
            BehaviorEvent.classroom_id == classroom_id,
        ).values(occurred_at=datetime(2026, 10, 21, 12, tzinfo=timezone.utc)))

    base = f"/classrooms/{classroom_id}/behavior?date_to=2026-10-22"
    first = client.get(base)
    assert first.status_code == 200
    assert len(re.findall(r"Event [0-9a-f-]{36}", first.text)) == 50
    first_chart = _chart_rows(first.text)
    assert len(first_chart) == 8
    assert sum(p + n for _, p, n in first_chart) == 54
    next_url_match = re.search(r'href="([^"]*before=[^"]+)"', first.text)
    assert next_url_match
    older = client.get(html.unescape(next_url_match.group(1)))
    assert older.status_code == 200
    assert len(re.findall(r"Event [0-9a-f-]{36}", older.text)) == 4
    assert _chart_rows(older.text) == first_chart
    assert "<strong>54</strong> total" in older.text


def test_weekly_report_authorization_empty_weeks_and_validation(engine):
    client, owner = _actor(engine)
    assistant_client, assistant = _actor(engine)
    outsider_client, _ = _actor(engine)
    classroom_id = _classroom(engine, owner)
    foreign_classroom = _classroom(engine, owner)
    _student(engine, owner, classroom_id)
    foreign_student = _student(engine, owner, foreign_classroom)
    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_id,
            user_id=assistant, role="assistant", created_by_user_id=owner,
        ))
    url = f"/classrooms/{classroom_id}/behavior?date_to=2026-10-22"
    response = client.get(url)
    assert response.status_code == 200
    rows = _chart_rows(response.text)
    assert len(rows) == 8
    assert all((positive, negative) == (0, 0) for _, positive, negative in rows)
    assert "<strong>0</strong> events in this chart window" in response.text
    assert _chart_rows(assistant_client.get(url).text) == rows
    assert outsider_client.get(url).status_code == 404
    assert TestClient(app, follow_redirects=False).get(url).status_code == 303
    assert client.get(url + f"&enrollment_id={foreign_student}").status_code == 404
    assert client.get(
        f"/classrooms/{classroom_id}/behavior?date_from=9999-12-31",
    ).status_code == 400
    for query in [
        "behavior_type=neutral", "date_to=9999-12-31",
        "date_from=2026-11-02&date_to=2026-10-22",
    ]:
        assert client.get(f"/classrooms/{classroom_id}/behavior?" + query).status_code == 400
