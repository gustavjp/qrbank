from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action
from app.models.audit import AdminAuditEvent
from app.models.behavior import BehaviorEvent, BehaviorReason
from app.models.classroom import ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user,
    verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.financial import post_manual_transaction, post_scanned_action
from app.services.students import create_student_enrollment


def _actor(engine):
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"reason-{uuid.uuid4()}@example.test",
            password="long-reason-management-password",
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


def _classroom(engine, owner, *, starting_balance=20):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner,
            name=f"Reasons {uuid.uuid4()}", school_year="2026-2027",
        )
        db.get(ClassroomSettings, classroom.id).starting_balance = starting_balance
        return classroom.id


def _student(engine, owner, classroom_id):
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner, classroom_id=classroom_id,
            student_code="01", display_name="Pupil", notes=None,
        )
        return record.enrollment.id


def _csrf(client):
    token = client.cookies.get(settings.csrf_cookie_name)
    assert token
    return token


def _reason(client, classroom_id, *, code="help", behavior_type="positive", name="Helping"):
    client.get(f"/classrooms/{classroom_id}/behavior/reasons")
    return client.post(f"/classrooms/{classroom_id}/behavior/reasons/new", data={
        "code": code, "name": name, "behavior_type": behavior_type,
        "description": "Kind conduct", "csrf_token": _csrf(client),
    })


def _manual_event(engine, owner, classroom_id, enrollment_id, *, positive=True):
    with Session(engine) as db, db.begin():
        posted = post_manual_transaction(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=enrollment_id,
            direction="credit" if positive else "debit", points=3,
            category="behavior_positive" if positive else "behavior_negative",
            note="Original ledger note", request_id=uuid.uuid4(),
        )
        db.flush()
        event = db.scalar(select(BehaviorEvent).where(
            BehaviorEvent.financial_transaction_id == posted.transaction.id,
        ))
        return event.id, posted.transaction.id


def _update(client, classroom_id, event_id, reason_id="", note="Detail"):
    return client.post(
        f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit",
        data={"reason_id": str(reason_id) if reason_id else "",
              "note": note, "csrf_token": _csrf(client)},
    )


def test_reason_catalog_create_edit_deactivate_and_audit(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    other_id = _classroom(engine, owner)
    assert _reason(client, classroom_id, code=" help ").status_code == 303
    assert _reason(client, other_id, code="help").status_code == 303

    with Session(engine) as db:
        reason = db.scalar(select(BehaviorReason).where(
            BehaviorReason.classroom_id == classroom_id,
        ))
        assert reason.reason_code == "HELP"
        assert reason.name == "Helping"
        assert reason.behavior_type == "positive"
        reason_id = reason.id
        assert db.scalar(select(func.count()).select_from(FinancialTransaction)) == 0

    base = f"/classrooms/{classroom_id}/behavior/reasons"
    assert client.post(base + f"/{reason_id}/edit", data={
        "code": " KIND ", "name": "Kindness", "description": "Helping others",
        "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(base + f"/{reason_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303

    with Session(engine) as db:
        edited = db.get(BehaviorReason, reason_id)
        assert edited.reason_code == "KIND"
        assert edited.behavior_type == "positive"
        assert edited.description == "Helping others"
        assert not edited.active
        events = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.target_id == reason_id,
        ).order_by(AdminAuditEvent.occurred_at, AdminAuditEvent.id)))
        assert {x.event_type for x in events} == {
            "behavior_reason_created", "behavior_reason_updated",
            "behavior_reason_deactivated",
        }
        update = next(x for x in events if x.event_type == "behavior_reason_updated")
        assert update.before_json["code"] == "HELP"
        assert update.after_json["code"] == "KIND"
        assert all(x.actor_user_id == owner for x in events)
    assert "Inactive" in client.get(base).text
    assert client.post(base + f"/{reason_id}/active", data={
        "active": "true", "csrf_token": _csrf(client),
    }).status_code == 303
    with Session(engine) as db:
        assert db.get(BehaviorReason, reason_id).active


def test_post_scan_reason_attachment_preserves_finance_and_audits_changes(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    assert _reason(client, classroom_id, code="HELP").status_code == 303
    with Session(engine) as db, db.begin():
        result = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        assert result.transaction is not None
        db.flush()
        event = db.scalar(select(BehaviorEvent).where(
            BehaviorEvent.financial_transaction_id == result.transaction.id,
        ))
        event_id, tx_id = event.id, result.transaction.id
        recorded_at = event.occurred_at

    with Session(engine) as db:
        before_tx = db.get(FinancialTransaction, tx_id)
        balance = db.get(Enrollment, student).current_balance
        tx_values = (before_tx.amount, before_tx.balance_before, before_tx.balance_after,
                     before_tx.note, before_tx.action_name_snapshot)

        reason_id = db.scalar(select(BehaviorReason.id).where(
            BehaviorReason.classroom_id == classroom_id,
        ))
    url = f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit"
    page = client.get(url)
    assert page.status_code == 200
    assert "Save behavior detail" in page.text
    assert "Helping" in page.text
    assert _update(client, classroom_id, event_id, reason_id, "Observed helping").status_code == 303

    with Session(engine) as db:
        attached = db.get(BehaviorEvent, event_id)
        assert attached.reason_code_snapshot == "HELP"
        assert attached.reason_name_snapshot == "Helping"
        assert attached.note == "Observed helping"
        assert attached.reason_attached_by_user_id == owner
        assert attached.reason_attached_at is not None
        assert attached.occurred_at == recorded_at
        tx = db.get(FinancialTransaction, tx_id)
        assert (tx.amount, tx.balance_before, tx.balance_after,
                tx.note, tx.action_name_snapshot) == tx_values
        assert db.get(Enrollment, student).current_balance == balance
        audits = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.target_type == "behavior_event",
            AdminAuditEvent.target_id == event_id,
        )))
        assert len(audits) == 1
        assert audits[0].before_json["reason_id"] is None
        assert audits[0].after_json["reason_id"] == str(reason_id)

    # Idempotent re-save does not alter snapshots, timestamps or audit count.
    assert _update(client, classroom_id, event_id, reason_id, "Observed helping").status_code == 303
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.target_type == "behavior_event",
            AdminAuditEvent.target_id == event_id,
        )) == 1
    history = client.get(f"/classrooms/{classroom_id}/behavior")
    assert "Helping" in history.text
    export = client.get(f"/classrooms/{classroom_id}/behavior/export.csv")
    assert "Observed helping" in export.text


def test_note_edit_preserves_historical_reason_snapshot_after_catalog_rename(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    assert _reason(client, classroom_id, code="TEAM", name="Teamwork").status_code == 303
    event_id, tx_id = _manual_event(engine, owner, classroom_id, student)
    with Session(engine) as db:
        reason_id = db.scalar(select(BehaviorReason.id).where(
            BehaviorReason.classroom_id == classroom_id,
        ))
    client.get(f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit")
    assert _update(client, classroom_id, event_id, reason_id, "First note").status_code == 303
    base = f"/classrooms/{classroom_id}/behavior/reasons"
    assert client.post(base + f"/{reason_id}/edit", data={
        "code": "GROUP", "name": "Cooperation", "description": "",
        "csrf_token": _csrf(client),
    }).status_code == 303
    assert client.post(base + f"/{reason_id}/active", data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    # Already-attached inactive reasons remain usable for note corrections.
    assert _update(client, classroom_id, event_id, reason_id, "Second note").status_code == 303

    with Session(engine) as db:
        event = db.get(BehaviorEvent, event_id)
        assert event.reason_code_snapshot == "TEAM"
        assert event.reason_name_snapshot == "Teamwork"
        assert event.note == "Second note"
        assert event.financial_transaction_id == tx_id
        audits = list(db.scalars(select(AdminAuditEvent).where(
            AdminAuditEvent.target_type == "behavior_event",
            AdminAuditEvent.target_id == event_id,
        ).order_by(AdminAuditEvent.occurred_at, AdminAuditEvent.id)))
        assert len(audits) == 2
        assert audits[-1].before_json["note"] == "First note"
        assert audits[-1].after_json["note"] == "Second note"
        assert audits[-1].after_json["reason_name"] == "Teamwork"
    page = client.get(f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit")
    assert "Recorded reason snapshot: TEAM — Teamwork" in page.text
    assert "Second note" in page.text
    assert _update(client, classroom_id, event_id, "", "").status_code == 303
    with Session(engine) as db:
        cleared = db.get(BehaviorEvent, event_id)
        assert cleared.behavior_reason_id is None
        assert cleared.reason_name_snapshot is None
        assert cleared.reason_code_snapshot is None
        assert cleared.note is None
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.target_type == "behavior_event",
            AdminAuditEvent.target_id == event_id,
        )) == 3


def test_type_tenant_inactive_and_csrf_guards(engine):
    client, owner = _actor(engine)
    outsider_client, outsider = _actor(engine)
    classroom_id = _classroom(engine, owner)
    foreign_classroom = _classroom(engine, outsider)
    student = _student(engine, owner, classroom_id)
    foreign_student = _student(engine, outsider, foreign_classroom)
    event_id, _ = _manual_event(engine, owner, classroom_id, student, positive=True)
    foreign_event_id, _ = _manual_event(
        engine, outsider, foreign_classroom, foreign_student, positive=True,
    )
    assert _reason(client, classroom_id, code="POS").status_code == 303
    assert _reason(client, classroom_id, code="NEG", behavior_type="negative").status_code == 303
    assert _reason(outsider_client, foreign_classroom, code="OTHER").status_code == 303
    with Session(engine) as db:
        reasons = {r.reason_code: r.id for r in db.scalars(select(BehaviorReason).where(
            BehaviorReason.classroom_id == classroom_id,
        ))}
        foreign_reason = db.scalar(select(BehaviorReason.id).where(
            BehaviorReason.classroom_id == foreign_classroom,
        ))
    base = f"/classrooms/{classroom_id}/behavior"
    client.get(base + f"/events/{event_id}/edit")
    assert _update(client, classroom_id, event_id, reasons["NEG"]).status_code == 400
    assert _update(client, classroom_id, event_id, foreign_reason).status_code == 400
    assert _update(client, classroom_id, foreign_event_id, reasons["POS"]).status_code == 404
    assert outsider_client.get(base + "/reasons").status_code == 404
    assert outsider_client.get(base + f"/events/{event_id}/edit").status_code == 404

    reason_url = base + f"/reasons/{reasons['POS']}/active"
    assert client.post(reason_url, data={
        "active": "false", "csrf_token": _csrf(client),
    }).status_code == 303
    assert _update(client, classroom_id, event_id, reasons["POS"]).status_code == 400
    assert client.post(base + f"/events/{event_id}/edit", data={
        "reason_id": "", "note": "no csrf", "csrf_token": "invalid",
    }).status_code == 403
    assert client.post(base + f"/events/{event_id}/edit", data={
        "reason_id": "bad-uuid", "note": "", "csrf_token": _csrf(client),
    }).status_code == 400
    with Session(engine) as db:
        event = db.get(BehaviorEvent, event_id)
        assert event.behavior_reason_id is None and event.note is None


def test_teacher_can_write_assistant_cannot_and_zero_point_events_supported(engine):
    owner_client, owner = _actor(engine)
    teacher_client, teacher = _actor(engine)
    assistant_client, assistant = _actor(engine)
    classroom_id = _classroom(engine, owner)
    student = _student(engine, owner, classroom_id)
    with Session(engine) as db, db.begin():
        db.add_all([
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom_id, user_id=teacher,
                role="teacher", created_by_user_id=owner,
            ),
            ClassroomMembership(
                id=uuid.uuid4(), classroom_id=classroom_id, user_id=assistant,
                role="assistant", created_by_user_id=owner,
            ),
        ])
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A05",
        ))
        action.amount = 0
        db.flush()
        result = post_scanned_action(
            db, actor_user_id=owner, classroom_id=classroom_id,
            enrollment_id=student, payload="QUICK:QA5",
            request_id=uuid.uuid4(),
        )
        assert result.transaction is None
        event_id = db.scalar(select(BehaviorEvent.id).where(
            BehaviorEvent.classroom_id == classroom_id,
            BehaviorEvent.enrollment_id == student,
        ))

    assert _reason(teacher_client, classroom_id, code="ZERO").status_code == 303
    with Session(engine) as db:
        reason_id = db.scalar(select(BehaviorReason.id).where(
            BehaviorReason.classroom_id == classroom_id,
        ))

    url = f"/classrooms/{classroom_id}/behavior/events/{event_id}/edit"
    assert assistant_client.get(url).status_code == 200
    assert "Save behavior detail" not in assistant_client.get(url).text
    assert assistant_client.get(f"/classrooms/{classroom_id}/behavior/reasons").status_code == 200
    assert "Create reason" not in assistant_client.get(
        f"/classrooms/{classroom_id}/behavior/reasons"
    ).text
    assistant_client.get(url)
    assert _update(assistant_client, classroom_id, event_id, reason_id).status_code == 403
    assert _reason(assistant_client, classroom_id, code="BAD").status_code == 403
    teacher_client.get(url)
    assert _update(teacher_client, classroom_id, event_id, reason_id, "No points").status_code == 303
    with Session(engine) as db:
        event = db.get(BehaviorEvent, event_id)
        assert event.reason_name_snapshot == "Helping"
        assert event.note == "No points"
        assert event.financial_transaction_id is None
        assert db.get(Enrollment, student).current_balance == 20
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == 1  # opening balance only


def test_code_validation_duplicates_and_failed_updates_leave_no_audit(engine):
    client, owner = _actor(engine)
    classroom_id = _classroom(engine, owner)
    assert _reason(client, classroom_id, code="OK").status_code == 303
    assert _reason(client, classroom_id, code="ok").status_code == 409
    assert _reason(client, classroom_id, code="bad code").status_code == 400
    assert _reason(client, classroom_id, code="POS2", behavior_type="unknown").status_code == 400
    assert _reason(client, classroom_id, code="LONG", name="x" * 121).status_code == 400
    with Session(engine) as db:
        reasons = list(db.scalars(select(BehaviorReason).where(
            BehaviorReason.classroom_id == classroom_id,
        )))
        assert len(reasons) == 1
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.target_type == "behavior_reason",
        )) == 1
