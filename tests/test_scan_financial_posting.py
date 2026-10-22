from __future__ import annotations

import re
import time
import uuid

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action
from app.models.behavior import BehaviorEvent
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialRequest, FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_scanned_action
from app.services.scan_selection import issue_selection_token, verify_selection_token, SelectionExpiredError
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"posting-{uuid.uuid4()}@example.test",
                             password="long-scan-password")
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id, *, starting_balance=0):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id, name=f"Posting {uuid.uuid4()}",
            school_year="2026-2027",
        )
        db.get(ClassroomSettings, classroom.id).starting_balance = starting_balance
        return classroom.id


def _student(engine, owner_id, classroom_id, code="01"):
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner_id, classroom_id=classroom_id,
            student_code=code, display_name=None, notes=None,
        )
        return record.enrollment.id


def _csrf(client):
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _start(client, classroom_id):
    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))
    response = client.get(f"/classrooms/{classroom_id}/scan")
    assert response.status_code == 200
    return response


def _request_id(page):
    match = re.search(r'name="request_id" value="([a-f0-9-]+)"', page.text)
    assert match, page.text
    return match.group(1)


def _execute(client, classroom_id, payload, student_id, request_id, *, selection_token=None):
    if student_id and selection_token is None:
        selection_token = issue_selection_token(
            session_token=client.cookies.get(settings.session_cookie_name),
            classroom_id=classroom_id, enrollment_id=student_id,
        )
    return client.post(f"/classrooms/{classroom_id}/scan/execute", data={
        "payload": payload, "selected_enrollment_id": str(student_id) if student_id else "",
        "selection_token": selection_token or "",
        "request_id": str(request_id), "csrf_token": _csrf(client),
    })


def _ledger(db, enrollment_id):
    return list(db.scalars(select(FinancialTransaction).where(
        FinancialTransaction.enrollment_id == enrollment_id,
    ).order_by(FinancialTransaction.display_number)))


def test_qr_scan_posts_integer_actions_and_historical_snapshots(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    page = _start(client, classroom_id)

    selected = _execute(client, classroom_id, "STUDENT:01", None, _request_id(page))
    assert selected.status_code == 200
    assert "Student 01 selected" in selected.text
    assert "TRANSACTION COMPLETED" not in selected.text

    first = _execute(client, classroom_id, "quick:qa1", student_id, _request_id(selected))
    assert first.status_code == 200
    assert "TRANSACTION COMPLETED" in first.text
    assert "New balance: 5" in first.text

    second = _execute(client, classroom_id, "ACTION:A02", student_id, _request_id(first))
    assert second.status_code == 200
    assert "New balance: 15" in second.text

    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 15
        first_tx, second_tx = _ledger(db, student_id)
        assert (first_tx.amount, first_tx.balance_before, first_tx.balance_after) == (5, 0, 5)
        assert (second_tx.amount, second_tx.balance_before, second_tx.balance_after) == (10, 5, 15)
        assert first_tx.source == second_tx.source == "scanner"
        assert first_tx.transaction_type == second_tx.transaction_type == "action"
        assert first_tx.quick_action_id is not None
        assert first_tx.quick_action_code_snapshot == "QA1"
        assert first_tx.action_code_snapshot == "A01"
        assert first_tx.action_name_snapshot == "General +5"
        assert first_tx.student_code_snapshot == "01"
        assert second_tx.quick_action_id is None
        assert second_tx.action_code_snapshot == "A02"

    with Session(engine) as db, db.begin():
        first_action = db.get(Action, first_tx.action_id)
        first_action.name = "Renamed later"
        first_action.amount = 99
    with Session(engine) as db:
        assert _ledger(db, student_id)[0].action_name_snapshot == "General +5"
        assert _ledger(db, student_id)[0].amount == 5


def test_duplicate_request_id_replays_and_fingerprint_collision_is_rejected(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    page = _start(client, classroom_id)
    request_id = _request_id(page)

    first = _execute(client, classroom_id, "QUICK:QA1", student_id, request_id)
    replay = _execute(client, classroom_id, " quick:qa1 ", student_id, request_id)
    assert first.status_code == replay.status_code == 200
    assert "No duplicate posting" in replay.text
    conflict = _execute(client, classroom_id, "QUICK:QA2", student_id, request_id)
    assert conflict.status_code == 409

    # A new logical request may legitimately repeat the same QR action when
    # the timing-based duplicate window is disabled (the classroom default).
    another = _execute(client, classroom_id, "QUICK:QA1", student_id, uuid.uuid4())
    assert another.status_code == 200
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 10
        assert len(_ledger(db, student_id)) == 2


def test_insufficient_balance_rejection_is_durable_and_replayable(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    student_id = _student(engine, owner, classroom_id)
    page = _start(client, classroom_id)
    request_id = _request_id(page)

    first = _execute(client, classroom_id, "QUICK:QA4", student_id, request_id)
    replay = _execute(client, classroom_id, "QUICK:QA4", student_id, request_id)
    assert first.status_code == replay.status_code == 422
    assert "Insufficient balance" in first.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        ledger = _ledger(db, student_id)
        assert len(ledger) == 1 and ledger[0].transaction_type == "opening_balance"
        request = db.scalar(select(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
            FinancialRequest.idempotency_key == request_id,
        ))
        assert request.status == "rejected"
        assert request.rejection_code == "insufficient_balance"


def test_tenant_role_status_and_csrf_do_not_allow_financial_writes(engine):
    client, owner = _actor(engine)
    outsider_client, outsider = _actor(engine)
    classroom_a = _classroom(engine, owner)
    classroom_b = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_a)
    student_b = _student(engine, owner, classroom_b)
    _start(client, classroom_a)
    outsider_client.cookies.set(settings.active_classroom_cookie_name, str(classroom_a))

    assert _execute(client, classroom_a, "QUICK:QA1", student_b, uuid.uuid4()).status_code == 400
    assert _execute(outsider_client, classroom_a, "QUICK:QA1", student_a, uuid.uuid4()).status_code == 404

    with Session(engine) as db, db.begin():
        db.add(ClassroomMembership(
            id=uuid.uuid4(), classroom_id=classroom_a, user_id=outsider,
            role="assistant", created_by_user_id=owner,
        ))
    assert _execute(outsider_client, classroom_a, "QUICK:QA1", student_a, uuid.uuid4()).status_code == 403

    response = client.post(f"/classrooms/{classroom_a}/scan/execute", data={
        "payload": "QUICK:QA1", "selected_enrollment_id": str(student_a),
        "request_id": str(uuid.uuid4()), "csrf_token": "tampered",
    })
    assert response.status_code == 403

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_a).active = False
    assert _execute(client, classroom_a, "QUICK:QA1", student_a, uuid.uuid4()).status_code == 400

    with Session(engine) as db:
        assert db.get(Enrollment, student_a).current_balance == 0
        assert not _ledger(db, student_a)


def test_confirmation_detects_changed_action_and_never_trusts_amount(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).confirm_normal_transactions = True
    page = _start(client, classroom_id)
    preview = _execute(client, classroom_id, "QUICK:QA1", student_id, _request_id(page))
    assert preview.status_code == 200
    assert "Confirm and post" in preview.text

    with Session(engine) as db, db.begin():
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A01",
        ))
        action.amount = 90
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', preview.text))
    confirm = client.post(f"/classrooms/{classroom_id}/scan/confirm", data=fields)
    assert confirm.status_code == 422
    assert "action changed since confirmation" in confirm.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 0
        assert not _ledger(db, student_id)


def test_behavior_events_and_zero_point_actions_do_not_fake_ledger_entries(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    _start(client, classroom_id)
    positive = _execute(client, classroom_id, "QUICK:QA5", student_id, uuid.uuid4())
    assert positive.status_code == 200

    with Session(engine) as db, db.begin():
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A07",
        ))
        action.amount = 0
    zero = _execute(client, classroom_id, "ACTION:A07", student_id, uuid.uuid4())
    assert zero.status_code == 200
    assert "0 points" in zero.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        assert len(_ledger(db, student_id)) == 1
        events = list(db.scalars(select(BehaviorEvent).where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.enrollment_id == student_id,
        )))
        assert {e.behavior_type for e in events} == {"positive", "negative"}
        assert len([e for e in events if e.financial_transaction_id is None]) == 1


def test_configured_duplicate_window_suppresses_without_double_posting(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).duplicate_scan_seconds = 120
    _start(client, classroom_id)
    first = _execute(client, classroom_id, "QUICK:QA1", student_id, uuid.uuid4())
    duplicate = _execute(client, classroom_id, "QUICK:QA1", student_id, uuid.uuid4())
    assert first.status_code == 200
    assert duplicate.status_code == 422
    assert "Duplicate scan suppressed" in duplicate.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        assert len(_ledger(db, student_id)) == 1


def test_concurrent_deductions_lock_balance_and_do_not_overdraw(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner, starting_balance=5)
    student_id = _student(engine, owner, classroom_id)
    gate = Barrier(2)

    def work():
        with Session(engine) as db:
            gate.wait(timeout=10)
            result = post_scanned_action(
                db, actor_user_id=owner, classroom_id=classroom_id,
                enrollment_id=student_id, payload="ACTION:A03",
                request_id=uuid.uuid4(),
            )
            db.commit()
            return result.rejection_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: work(), range(2)))
    assert results.count(None) == 1
    assert results.count("insufficient_balance") == 1
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 0
        ledger = _ledger(db, student_id)
        assert [tx.amount for tx in ledger] == [5, -5]
        assert ledger[1].balance_before == 5 and ledger[1].balance_after == 0


def test_uncommitted_action_rolls_back_ledger_request_and_balance(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    request_id = uuid.uuid4()
    with Session(engine) as db:
        result = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, payload="ACTION:A01", request_id=request_id,
        )
        assert result.transaction is not None
        db.rollback()
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 0
        assert not _ledger(db, student_id)
        assert db.scalar(select(func.count()).select_from(FinancialRequest).where(
            FinancialRequest.classroom_id == classroom_id,
        )) == 0


def test_confirmed_normal_action_posts_exactly_once(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).confirm_normal_transactions = True
    page = _start(client, classroom_id)
    preview = _execute(client, classroom_id, "ACTION:A01", student_id, _request_id(page))
    assert preview.status_code == 200
    with Session(engine) as db:
        assert not _ledger(db, student_id)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', preview.text))
    posted = client.post(f"/classrooms/{classroom_id}/scan/confirm", data=fields)
    replay = client.post(f"/classrooms/{classroom_id}/scan/confirm", data=fields)
    assert posted.status_code == replay.status_code == 200
    assert "TRANSACTION COMPLETED" in posted.text
    assert "No duplicate posting" in replay.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        assert len(_ledger(db, student_id)) == 1


def test_concurrent_exact_same_request_posts_only_once(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    request_id = uuid.uuid4()
    gate = Barrier(2)

    def work():
        with Session(engine) as db:
            gate.wait(timeout=10)
            result = post_scanned_action(
                db, actor_user_id=owner, classroom_id=classroom_id,
                enrollment_id=student_id, payload="QUICK:QA1",
                request_id=request_id,
            )
            db.commit()
            return result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: work(), range(2)))
    assert sorted(results) == [False, True]
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        assert len(_ledger(db, student_id)) == 1


def test_replay_remains_safe_after_action_is_deactivated(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    _start(client, classroom_id)
    request_id = uuid.uuid4()
    first = _execute(client, classroom_id, "ACTION:A01", student_id, request_id)
    assert first.status_code == 200
    with Session(engine) as db, db.begin():
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id,
            Action.action_code == "A01",
        ))
        action.active = False
    replay = _execute(client, classroom_id, "ACTION:A01", student_id, request_id)
    rejected_new = _execute(client, classroom_id, "ACTION:A01", student_id, uuid.uuid4())
    assert replay.status_code == 200
    assert "No duplicate posting" in replay.text
    assert rejected_new.status_code == 400
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 5
        assert len(_ledger(db, student_id)) == 1


def test_preloaded_enrollment_is_refreshed_after_row_lock(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as stale_session:
        stale_enrollment = stale_session.get(Enrollment, student_id)
        assert stale_enrollment.current_balance == 0
        with Session(engine) as other_session:
            post_scanned_action(
                other_session, actor_user_id=owner, classroom_id=classroom_id,
                enrollment_id=student_id, payload="ACTION:A01",
                request_id=uuid.uuid4(),
            )
            other_session.commit()
        # The first session's identity map still contains the pre-post balance.
        assert stale_enrollment.current_balance == 0
        posted = post_scanned_action(
            stale_session, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student_id, payload="ACTION:A01",
            request_id=uuid.uuid4(),
        )
        assert posted.transaction.balance_before == 5
        assert posted.transaction.balance_after == 10
        stale_session.commit()
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 10
        assert [tx.amount for tx in _ledger(db, student_id)] == [5, 5]


def test_student_scan_issues_bound_selection_token_before_financial_action(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    page = _start(client, classroom_id)
    selected = _execute(client, classroom_id, "STUDENT:01", None, _request_id(page))
    assert selected.status_code == 200
    assert 'data-tone="student"' in selected.text
    assert "Selection expires in" in selected.text or 'id="selection-countdown"' in selected.text
    match = re.search(r'name="selection_token" value="([^"]+)"', selected.text)
    assert match and match.group(1)
    token = match.group(1)
    verify_selection_token(
        session_token=client.cookies.get(settings.session_cookie_name),
        classroom_id=classroom_id, enrollment_id=student_id,
        selection_token=token, timeout_seconds=120,
    )
    posted = _execute(
        client, classroom_id, "QUICK:QA1", student_id, uuid.uuid4(),
        selection_token=token,
    )
    assert posted.status_code == 200
    assert 'data-tone="success"' in posted.text
    assert 'id="selection-countdown"' in posted.text


def test_selection_token_rejects_tampering_wrong_student_or_expiry(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    first_student = _student(engine, owner, classroom_id, "01")
    second_student = _student(engine, owner, classroom_id, "02")
    _start(client, classroom_id)
    token = issue_selection_token(
        session_token=client.cookies.get(settings.session_cookie_name),
        classroom_id=classroom_id, enrollment_id=first_student,
    )
    wrong_student = _execute(
        client, classroom_id, "ACTION:A01", second_student, uuid.uuid4(),
        selection_token=token,
    )
    assert wrong_student.status_code == 409
    forged = _execute(
        client, classroom_id, "ACTION:A01", first_student, uuid.uuid4(),
        selection_token=token[:-1] + ("a" if token[-1] != "a" else "b"),
    )
    assert forged.status_code == 409
    old_token = issue_selection_token(
        session_token=client.cookies.get(settings.session_cookie_name),
        classroom_id=classroom_id, enrollment_id=first_student,
        now=int(time.time()) - 121,
    )
    expired = _execute(
        client, classroom_id, "ACTION:A01", first_student, uuid.uuid4(),
        selection_token=old_token,
    )
    assert expired.status_code == 409
    assert "student selection expired" in expired.text
    with Session(engine) as db:
        assert db.get(Enrollment, first_student).current_balance == 0
        assert db.get(Enrollment, second_student).current_balance == 0
        assert not _ledger(db, first_student)
        assert not _ledger(db, second_student)


def test_selection_signature_is_bound_to_session_classroom_and_deadline():
    classroom_id, another_classroom = uuid.uuid4(), uuid.uuid4()
    enrollment_id, another_student = uuid.uuid4(), uuid.uuid4()
    token = issue_selection_token(
        session_token="session-one", classroom_id=classroom_id,
        enrollment_id=enrollment_id, now=1000,
    )
    assert verify_selection_token(
        session_token="session-one", classroom_id=classroom_id,
        enrollment_id=enrollment_id, selection_token=token,
        timeout_seconds=10, now=1009,
    ) == 1010
    cases = [
        dict(session_token="session-two", classroom_id=classroom_id,
             enrollment_id=enrollment_id, now=1002),
        dict(session_token="session-one", classroom_id=another_classroom,
             enrollment_id=enrollment_id, now=1002),
        dict(session_token="session-one", classroom_id=classroom_id,
             enrollment_id=another_student, now=1002),
        dict(session_token="session-one", classroom_id=classroom_id,
             enrollment_id=enrollment_id, now=1010),
        dict(session_token="session-one", classroom_id=classroom_id,
             enrollment_id=enrollment_id, now=999),
    ]
    for kwargs in cases:
        with pytest.raises(SelectionExpiredError):
            verify_selection_token(
                **kwargs, selection_token=token, timeout_seconds=10,
            )


def test_expired_confirm_form_never_posts_even_if_preview_was_valid(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        row = db.get(ClassroomSettings, classroom_id)
        row.confirm_normal_transactions = True
        row.student_selection_timeout_seconds = 5
    page = _start(client, classroom_id)
    preview = _execute(client, classroom_id, "ACTION:A01", student_id, _request_id(page))
    assert preview.status_code == 200 and "Confirm and post" in preview.text
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', preview.text))
    fields["selection_token"] = issue_selection_token(
        session_token=client.cookies.get(settings.session_cookie_name),
        classroom_id=classroom_id, enrollment_id=student_id,
        now=int(time.time()) - 6,
    )
    denied = client.post(f"/classrooms/{classroom_id}/scan/confirm", data=fields)
    assert denied.status_code == 409
    assert "student selection expired" in denied.text
    with Session(engine) as db:
        assert db.get(Enrollment, student_id).current_balance == 0
        assert not _ledger(db, student_id)


def test_sound_setting_is_reflected_in_scanner_page_and_replays_are_quiet(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    page = _start(client, classroom_id)
    assert 'data-sound-enabled="1"' in page.text
    request_id = uuid.uuid4()
    posted = _execute(client, classroom_id, "ACTION:A01", student_id, request_id)
    replay = _execute(client, classroom_id, "ACTION:A01", student_id, request_id)
    assert 'data-tone="success"' in posted.text
    assert 'data-tone="none"' in replay.text
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).sound_enabled = False
    disabled = client.get(f"/classrooms/{classroom_id}/scan")
    assert disabled.status_code == 200
    assert 'data-sound-enabled="0"' in disabled.text
    assert "Sound feedback is disabled" in disabled.text
