from __future__ import annotations

import csv
import uuid
from io import BytesIO, StringIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.action import Action, QuickAction
from app.models.financial import FinancialTransaction
from app.services.auth import (
    create_email_verification_token, create_user_session,
    register_user, verify_email_token,
)
from app.services.classrooms import create_classroom_for_owner
from app.services.qr_archives import ArchiveQr, build_qr_archive
from app.services.qr_codes import qr_command, render_qr
from app.services.students import create_student_enrollment


def _client(engine):
    with Session(engine) as db, db.begin():
        user = register_user(db, email=f"qr-zip-{uuid.uuid4()}@example.test",
                             password="qr-test-password")
        token = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=token.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, session_token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, session_token)
    client.get("/dashboard")
    return client, user_id


def _setup(engine, owner_id):
    with Session(engine) as db, db.begin():
        classroom = create_classroom_for_owner(
            db, owner_user_id=owner_id, name="Room 4B", school_year="2026-2027",
        )
        student = create_student_enrollment(
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
        return classroom.id, student.id, quick.id, action.id


def _files(response):
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "manifest.csv" in names
        manifest = list(csv.DictReader(StringIO(
            archive.read("manifest.csv").decode("utf-8-sig")
        )))
        files = {name: archive.read(name) for name in names if name != "manifest.csv"}
    return files, manifest


@pytest.mark.parametrize("image_format", ["png", "svg"])
def test_zip_service_has_scannable_images_and_manifest(image_format):
    items = [
        ArchiveQr("student", uuid.uuid4(), "01", "Student 01"),
        ArchiveQr("quick", uuid.uuid4(), "QA1", "Quick QA1"),
        ArchiveQr("action", uuid.uuid4(), "A01", "Action A01"),
    ]
    content = build_qr_archive(items, image_format=image_format,
                               classroom_name="Room 4B", school_year="2026-2027")
    with ZipFile(BytesIO(content)) as archive:
        names = archive.namelist()
        assert len(names) == 4
        assert len(set(names)) == len(names)
        rows = list(csv.DictReader(StringIO(
            archive.read("manifest.csv").decode("utf-8-sig")
        )))
        assert len(rows) == 3
        for row, item in zip(rows, items):
            assert row["type"] == item.kind
            assert row["code"] == item.code
            assert row["command"] == qr_command(item.kind, item.code)
            assert row["classroom"] == "Room 4B"
            assert row["school_year"] == "2026-2027"
            assert archive.read(row["file"]) == render_qr(row["command"], image_format)
            assert row["file"].endswith(f".{image_format}")
            assert ".." not in row["file"]


def test_zip_service_rejects_invalid_and_unsafe_entries():
    item = ArchiveQr("student", uuid.uuid4(), "01", "Student 01")
    params = {"image_format": "svg", "classroom_name": "Room", "school_year": "2026"}
    with pytest.raises(ValueError):
        build_qr_archive([], **params)
    with pytest.raises(ValueError):
        build_qr_archive([item] * 121, **params)
    with pytest.raises(ValueError):
        build_qr_archive([item, item], **params)
    with pytest.raises(ValueError):
        build_qr_archive([item], **{**params, "image_format": "pdf"})
    with pytest.raises(ValueError):
        build_qr_archive([ArchiveQr("unknown", uuid.uuid4(), "01", "Bad")], **params)

    # Malicious-looking labels and classroom names cannot become executable CSV cells.
    content = build_qr_archive([item], image_format="svg", classroom_name="=2+2",
                               school_year="2026")
    with ZipFile(BytesIO(content)) as archive:
        text = archive.read("manifest.csv").decode("utf-8-sig")
    assert "'=2+2" in text


def test_zip_endpoint_all_selected_scoped_and_read_only(engine):
    client, owner = _client(engine)
    classroom_id, student_id, quick_id, action_id = _setup(engine, owner)
    root = f"/classrooms/{classroom_id}/qr"
    page = client.get(root)
    assert page.status_code == 200
    assert "Download All (ZIP)" in page.text
    assert "Download Selected (ZIP)" in page.text
    csrf = client.cookies.get(settings.csrf_cookie_name)

    data = {"csrf_token": csrf, "image_format": "png", "selection": "all"}
    files, rows = _files(client.post(f"{root}/export.zip", data=data))
    # Classrooms seed additional default actions and Quick Actions.
    assert len(files) == len(rows) >= 3
    assert {"STUDENT:01", "QUICK:QA1", "ACTION:A01"}.issubset({
        row["command"] for row in rows
    })
    assert all(value.startswith(b"\x89PNG\r\n\x1a\n") for value in files.values())

    selected = {**data, "selection": "selected", "image_format": "svg",
                "student_ids": str(student_id), "action_ids": str(action_id)}
    files, rows = _files(client.post(f"{root}/export.zip", data=selected))
    assert len(files) == 2
    assert {row["command"] for row in rows} == {"STUDENT:01", "ACTION:A01"}
    assert all(b"<svg" in value for value in files.values())

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == 0

    guest = TestClient(app, follow_redirects=False)
    assert guest.post(f"{root}/export.zip", data=selected).status_code == 303
    outsider, _ = _client(engine)
    assert outsider.post(f"{root}/export.zip", data=selected).status_code == 404
    assert client.post(f"{root}/export.zip", data={**selected,
        "action_ids": str(uuid.uuid4())}).status_code == 404
    assert client.post(f"{root}/export.zip", data={**selected,
        "csrf_token": "invalid"}).status_code in {400, 403}
    assert client.post(f"{root}/export.zip", data={**selected,
        "image_format": "jpeg"}).status_code == 400
    assert client.post(f"{root}/export.zip", data={**selected,
        "selection": "unknown"}).status_code == 400
    assert client.post(f"{root}/export.zip", data={**data,
        "selection": "selected"}).status_code == 400

    with Session(engine) as db, db.begin():
        db.get(QuickAction, quick_id).active = False
    assert client.post(f"{root}/export.zip", data={**selected,
        "quick_ids": str(quick_id)}).status_code == 404
