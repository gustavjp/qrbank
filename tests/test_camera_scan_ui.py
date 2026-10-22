from __future__ import annotations

import uuid
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.classroom import ClassroomSettings
from app.models.financial import FinancialTransaction
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.students import create_student_enrollment


def _client(engine):
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"camera-{uuid.uuid4()}@example.test",
                             password="camera-test-password")
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _classroom(engine, owner_id):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id, name="Camera Room", school_year="2026-2027",
        )
        enrollment = create_student_enrollment(
            db, user_id=owner_id, classroom_id=classroom.id,
            student_code="01", display_name=None, notes=None,
        ).enrollment
        return classroom.id, enrollment.id


def test_camera_controls_share_the_secure_scan_form(engine):
    client, owner = _client(engine)
    classroom_id, _ = _classroom(engine, owner)
    root = f"/classrooms/{classroom_id}/scan"
    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))
    response = client.get(root)
    assert response.status_code == 200
    html = response.text
    assert 'id="scanner-input" name="payload"' in html
    assert f'action="{root}/execute"' in html
    assert 'id="camera-start"' in html
    assert 'id="camera-stop"' in html
    assert 'id="camera-video" autoplay muted playsinline' in html
    # The video is outside the <main> element replaced after each scan.
    assert html.index('id="camera-video"') > html.index("</main>")
    assert 'id="camera-status" role="status" aria-live="polite"' in html
    assert "form.requestSubmit()" in html
    assert "window.isSecureContext" in html
    assert "cameraControls.stop()" in html
    assert 'window.addEventListener("pagehide", stopCamera)' in html
    assert "heldCameraPayload" in html
    assert "@zxing/browser@0.2.1/umd/zxing-browser.min.js" in html
    assert "sha384-HRtzk9lZgkbSgvUyQrnfC/GxiXZgwaNyD7hC9wcXlsBpDhkS80ISl73juef2FRuf" in html
    assert 'script.referrerPolicy = "no-referrer"' in html


def test_camera_payload_uses_existing_authorization_and_selection(engine):
    client, owner = _client(engine)
    classroom_id, student_id = _classroom(engine, owner)
    root = f"/classrooms/{classroom_id}/scan"
    client.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))
    assert client.get(root).status_code == 200
    csrf = client.cookies.get(settings.csrf_cookie_name)

    # A decoded action must still be refused until a student has been scanned.
    response = client.post(f"{root}/execute", data={
        "csrf_token": csrf, "payload": "QUICK:QA1", "request_id": str(uuid.uuid4()),
    })
    assert response.status_code == 400
    assert "scan a student before scanning" in response.text

    selected = client.post(f"{root}/execute", data={
        "csrf_token": csrf, "payload": "STUDENT:01", "request_id": str(uuid.uuid4()),
    })
    assert selected.status_code == 200
    assert "Student 01 selected" in selected.text
    assert 'id="camera-start"' in selected.text

    # Confirmation still uses the original financial flow, and the scanner
    # controller deliberately ignores camera frames while this form is shown.
    match = re.search(r'name="selection_token" value="([^"]+)"', selected.text)
    assert match
    with Session(engine) as db, db.begin():
        db.get(ClassroomSettings, classroom_id).confirm_normal_transactions = True
    confirmation = client.post(f"{root}/execute", data={
        "csrf_token": csrf, "payload": "QUICK:QA1",
        "selected_enrollment_id": str(student_id),
        "selection_token": match.group(1), "request_id": str(uuid.uuid4()),
    })
    assert confirmation.status_code == 200
    assert f'action="{root}/confirm"' in confirmation.text
    assert 'id="camera-start"' not in confirmation.text
    assert 'id="camera-video"' in confirmation.text

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.enrollment_id == student_id,
        )) == 0

    other, _ = _client(engine)
    other.cookies.set(settings.active_classroom_cookie_name, str(classroom_id))
    assert other.get(root).status_code == 404
    assert TestClient(app, follow_redirects=False).get(root).status_code == 303


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_camera_browser_controller_handoff():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["node", "tests/camera_scan_harness.cjs"], cwd=root,
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
