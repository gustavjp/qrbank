from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action, QuickAction
from app.models.financial import FinancialTransaction
from app.models.student import Enrollment
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.qr_codes import qr_command, render_qr
from app.services.students import create_student_enrollment


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"qr-{uuid.uuid4()}@example.test", password="qr-test-password",
        )
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _classroom_with_student(engine, owner_id: uuid.UUID):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id,
            name=f"Room {uuid.uuid4()}", school_year="2026-2027",
        )
        enrollment = create_student_enrollment(
            db, user_id=owner_id, classroom_id=classroom.id,
            student_code="01", display_name=None, notes=None,
        ).enrollment
        quick = db.scalar(select(QuickAction).where(
            QuickAction.classroom_id == classroom.id,
            QuickAction.quick_action_code == "QA1",
        ))
        action = db.scalar(select(Action).where(
            Action.classroom_id == classroom.id,
            Action.action_code == "A01",
        ))
        return classroom.id, enrollment.id, quick.id, action.id


def test_qr_encoding_is_local_deterministic_and_scanner_compatible():
    assert qr_command("student", "01") == "STUDENT:01"
    assert qr_command("quick", "qa1") == "QUICK:QA1"
    assert qr_command("action", "A01") == "ACTION:A01"
    png = render_qr("STUDENT:01", "png")
    svg = render_qr("STUDENT:01", "svg")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"<svg" in svg
    assert png == render_qr("STUDENT:01", "png")
    assert svg == render_qr("STUDENT:01", "svg")


def test_qr_center_lists_current_codes_and_downloads_without_transactions(engine):
    client, owner = _client(engine)
    classroom_id, student_id, quick_id, action_id = _classroom_with_student(engine, owner)
    root = f"/classrooms/{classroom_id}/qr"

    page = client.get(root)
    assert page.status_code == 200
    assert "STUDENT:01" in page.text
    assert "QUICK:QA1" in page.text
    assert "ACTION:A01" in page.text
    assert "2026-2027" in page.text
    assert "Download SVG" in page.text and "Download PNG" in page.text
    assert page.headers["cache-control"] == "private, no-store"

    for kind, entity_id in (
        ("student", student_id), ("quick", quick_id), ("action", action_id),
    ):
        svg = client.get(f"{root}/{kind}/{entity_id}/svg")
        assert svg.status_code == 200 and b"<svg" in svg.content
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert svg.headers["x-content-type-options"] == "nosniff"
        assert svg.headers["cache-control"] == "private, no-store"
        png = client.get(f"{root}/{kind}/{entity_id}/png?download=true")
        assert png.status_code == 200
        assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert png.headers["content-disposition"].startswith("attachment;")

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == 0


def test_qr_downloads_are_tenant_scoped_and_require_login(engine):
    owner_client, owner = _client(engine)
    classroom_id, student_id, quick_id, action_id = _classroom_with_student(engine, owner)
    other_client, _ = _client(engine)
    root = f"/classrooms/{classroom_id}/qr"

    assert other_client.get(root).status_code == 404
    for kind, entity_id in (
        ("student", student_id), ("quick", quick_id), ("action", action_id),
    ):
        assert other_client.get(f"{root}/{kind}/{entity_id}/svg").status_code == 404
    guest = TestClient(app, follow_redirects=False)
    assert guest.get(root).status_code == 303
    assert guest.get(f"{root}/student/{student_id}/png").status_code == 303
    assert owner_client.get(f"{root}/student/{uuid.uuid4()}/svg").status_code == 404
    assert owner_client.get(f"{root}/student/{student_id}/gif").status_code == 404


def test_inactive_entities_cannot_be_downloaded_and_new_codes_are_used(engine):
    client, owner = _client(engine)
    classroom_id, student_id, quick_id, action_id = _classroom_with_student(engine, owner)
    root = f"/classrooms/{classroom_id}/qr"

    with Session(engine) as db, db.begin():
        enrollment = db.get(Enrollment, student_id)
        enrollment.student_code = "02"
        quick = db.get(QuickAction, quick_id)
        quick.active = False
        action = db.get(Action, action_id)
        action.active = False

    page = client.get(root)
    assert "STUDENT:02" in page.text and "STUDENT:01" not in page.text
    assert "<code>QUICK:QA1</code>" not in page.text
    assert "ACTION:A01" not in page.text
    assert client.get(f"{root}/student/{student_id}/svg").status_code == 200
    assert client.get(f"{root}/quick/{quick_id}/svg").status_code == 404
    assert client.get(f"{root}/action/{action_id}/svg").status_code == 404

    with Session(engine) as db, db.begin():
        db.get(Enrollment, student_id).active = False
    assert client.get(f"{root}/student/{student_id}/svg").status_code == 404
