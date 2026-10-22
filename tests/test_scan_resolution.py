from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action, QuickAction
from app.models.audit import AdminAuditEvent
from app.models.classroom import Classroom, ClassroomMembership, ClassroomSettings
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.actions import DEFAULT_ACTIONS, ActionValidationError, install_default_actions
from app.services.auth import (
    create_email_verification_token, create_user_session, register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.students import create_student_enrollment


PASSWORD = "scan-preview-test-password"


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"scan-{uuid.uuid4()}@example.test", password=PASSWORD)
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id, name=f"Scan class {uuid.uuid4()}",
            school_year="2026-2027",
        )
        return classroom.id


def _student(engine, owner_id: uuid.UUID, classroom_id: uuid.UUID, code="01") -> uuid.UUID:
    with Session(engine) as db, db.begin():
        record = create_student_enrollment(
            db, user_id=owner_id, classroom_id=classroom_id,
            student_code=code, display_name=None, notes=None,
        )
        return record.enrollment.id


def _csrf(client: TestClient) -> str:
    csrf = client.cookies.get(settings.csrf_cookie_name)
    assert csrf
    return csrf


def _scan(client: TestClient, classroom_id: uuid.UUID, payload: str, selected: uuid.UUID | None = None):
    return client.post(f"/classrooms/{classroom_id}/scan/resolve", data={
        "payload": payload,
        "selected_enrollment_id": str(selected) if selected else "",
        "csrf_token": _csrf(client),
    })


def test_new_classroom_has_ten_default_quick_mappings_without_balance_changes(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)

    with Session(engine) as db:
        actions = {action.action_code: action for action in db.scalars(
            select(Action).where(Action.classroom_id == classroom_id)
        )}
        quicks = {quick.quick_action_code: quick for quick in db.scalars(
            select(QuickAction).where(QuickAction.classroom_id == classroom_id)
        )}
        assert len(actions) == len(quicks) == 10
        for index, (action_code, quick_code, name, kind, amount) in enumerate(DEFAULT_ACTIONS, start=1):
            action = actions[action_code]
            quick = quicks[quick_code]
            assert (action.name, action.action_kind, action.amount, action.active) == (
                name, kind, amount, True,
            )
            assert (quick.action_id, quick.display_order, quick.active) == (action.id, index, True)
        assert db.scalar(select(func.count()).select_from(AdminAuditEvent).where(
            AdminAuditEvent.classroom_id == classroom_id,
            AdminAuditEvent.event_type == "default_actions_installed",
        )) == 1
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == 0

    # Reinstall never overwrites teacher edits or posts a financial transaction.
    with Session(engine) as db, db.begin():
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_id, Action.action_code == "A01",
        ))
        action.amount = 13
        assert install_default_actions(db, user_id=owner, classroom_id=classroom_id) is False
    with Session(engine) as db:
        assert db.scalar(select(Action.amount).where(
            Action.classroom_id == classroom_id, Action.action_code == "A01",
        )) == 13


def test_existing_custom_codes_block_partial_default_setup_without_overwrite(engine) -> None:
    client, owner = _client(engine)
    with Session(engine) as db, db.begin():
        classroom = Classroom(id=uuid.uuid4(), name="Legacy class", school_year="2026-2027")
        db.add(classroom)
        db.flush()
        db.add_all([
            ClassroomSettings(classroom_id=classroom.id),
            ClassroomMembership(id=uuid.uuid4(), classroom_id=classroom.id,
                                user_id=owner, role="owner", created_by_user_id=owner),
            Action(id=uuid.uuid4(), classroom_id=classroom.id,
                   action_code="A01", name="Custom reward", action_kind="general", amount=77),
        ])
        classroom_id = classroom.id

    client.get("/dashboard")
    response = client.post(f"/classrooms/{classroom_id}/actions/defaults", data={
        "csrf_token": _csrf(client),
    })
    assert response.status_code == 400
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Action).where(
            Action.classroom_id == classroom_id,
        )) == 1
        assert db.scalar(select(func.count()).select_from(QuickAction).where(
            QuickAction.classroom_id == classroom_id,
        )) == 0


def test_scan_selects_active_student_then_previews_shortcut_without_posting(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    student_id = _student(engine, owner, classroom_id)
    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))

    page = client.get(f"/classrooms/{classroom_id}/scan")
    assert page.status_code == 200
    assert "Preview-only milestone" in page.text
    assert _scan(client, classroom_id, "QUICK:QA1").status_code == 400

    selected = _scan(client, classroom_id, "student:01")
    assert selected.status_code == 200
    assert "Student 01 selected" in selected.text

    quick = _scan(client, classroom_id, " quick:qa1 ", student_id)
    assert quick.status_code == 200
    assert "General +5" in quick.text
    assert "No balance change was made" in quick.text

    direct = _scan(client, classroom_id, "ACTION:A02", student_id)
    assert direct.status_code == 200
    assert "General +10" in direct.text

    with Session(engine) as db:
        enrollment = db.get(Enrollment, student_id)
        assert enrollment.current_balance == 0
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_id,
        )) == 0


def test_scan_rejects_wrong_tenant_and_inactive_entities(engine) -> None:
    client, owner = _client(engine)
    outsider, _ = _client(engine)
    classroom_a = _classroom(engine, owner)
    classroom_b = _classroom(engine, owner)
    student_a = _student(engine, owner, classroom_a)
    student_b = _student(engine, owner, classroom_b)

    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_a))
    assert _scan(client, classroom_a, "QUICK:QA1", student_b).status_code == 400
    assert _scan(client, classroom_a, "STUDENT:01").status_code == 200
    assert _scan(outsider, classroom_a, "STUDENT:01").status_code == 404
    assert client.get(f"/classrooms/{classroom_b}/scan").status_code == 409

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_a).active = False
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom_a, Action.action_code == "A01",
        ))
        action.active = False
    assert _scan(client, classroom_a, "STUDENT:01").status_code == 400
    assert _scan(client, classroom_a, "QUICK:QA1", student_a).status_code == 400

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_a).active = True
    assert _scan(client, classroom_a, "QUICK:QA1", student_a).status_code == 400
    assert _scan(client, classroom_a, "ACTION:A01", student_a).status_code == 400


def test_scanner_rejects_bad_payload_and_csrf(engine) -> None:
    client, owner = _client(engine)
    classroom_id = _classroom(engine, owner)
    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))
    client.get(f"/classrooms/{classroom_id}/scan")
    for payload in ("STUDENT:bad code", "QUICK:QA1:extra", "random", "ACTION:"):
        assert _scan(client, classroom_id, payload).status_code == 400
    response = client.post(f"/classrooms/{classroom_id}/scan/resolve", data={
        "payload": "STUDENT:01", "csrf_token": "invalid",
    })
    assert response.status_code == 403
