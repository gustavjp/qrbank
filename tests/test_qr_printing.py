from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
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
from app.services.qr_printing import PrintableQr, generate_printable_pdf
from app.services.students import create_student_enrollment


def _client(engine) -> tuple[TestClient, uuid.UUID]:
    with Session(engine) as db, db.begin():
        user = register_user(
            db, email=f"qr-print-{uuid.uuid4()}@example.test", password="qr-test-password",
        )
        verification = create_email_verification_token(db, user_id=user.id)
        verify_email_token(db, token=verification.token)
        session = create_user_session(db, user_id=user.id, user_agent="pytest")
        user_id, token = user.id, session.token
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(settings.session_cookie_name, token)
    client.get("/dashboard")
    return client, user_id


def _setup(engine, owner_id: uuid.UUID):
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


def _reader(response):
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    return PdfReader(BytesIO(response.content))


def test_pdf_renderer_letter_a4_and_multiple_pages():
    student_items = [PrintableQr("student", f"{i:02}", f"Student {i:02}")
                     for i in range(1, 8)]
    letter = PdfReader(BytesIO(generate_printable_pdf(
        student_items, classroom_name="Room 4B", school_year="2026-2027",
        paper="letter", layout="student_cards",
    )))
    assert len(letter.pages) == 2  # Two columns, three rows per page.
    assert round(float(letter.pages[0].mediabox.width)) == 612
    assert round(float(letter.pages[0].mediabox.height)) == 792
    assert "Room 4B" in letter.pages[0].extract_text()
    assert "STUDENT:01" in letter.pages[0].extract_text()
    assert "STUDENT:07" in letter.pages[1].extract_text()

    mixed = [PrintableQr("student", "01", "Student 01"),
             PrintableQr("quick", "QA1", "Quick QA1"),
             PrintableQr("action", "A01", "Action A01")]
    a4 = PdfReader(BytesIO(generate_printable_pdf(
        mixed, classroom_name="Room 4B", school_year="2026-2027",
        paper="a4", layout="qr_only",
    )))
    assert len(a4.pages) == 1
    assert round(float(a4.pages[0].mediabox.width)) == 595
    assert round(float(a4.pages[0].mediabox.height)) == 842
    text = a4.pages[0].extract_text()
    assert "STUDENT:01" in text and "QUICK:QA1" in text and "ACTION:A01" in text


def test_pdf_renderer_rejects_invalid_or_unbounded_selection():
    with pytest.raises(ValueError):
        generate_printable_pdf([], classroom_name="Room", school_year="2026",
                               paper="letter", layout="student_cards")
    with pytest.raises(ValueError):
        generate_printable_pdf(
            [PrintableQr("quick", "QA1", "Quick QA1")],
            classroom_name="Room", school_year="2026",
            paper="letter", layout="student_cards",
        )
    with pytest.raises(ValueError):
        generate_printable_pdf(
            [PrintableQr("student", "01", "Student 01")],
            classroom_name="Room", school_year="2026",
            paper="legal", layout="student_cards",
        )


def test_pdf_route_all_and_selected_are_secure_and_read_only(engine):
    client, owner = _client(engine)
    classroom_id, student_id, quick_id, action_id = _setup(engine, owner)
    root = f"/classrooms/{classroom_id}/qr"
    center = client.get(root)
    assert center.status_code == 200
    assert "Print All for Layout" in center.text
    csrf = client.cookies.get(settings.csrf_cookie_name)
    assert csrf
    data = {"csrf_token": csrf, "paper": "letter", "layout": "student_cards",
            "selection": "all"}
    pdf = _reader(client.post(f"{root}/print.pdf", data=data))
    assert "STUDENT:01" in pdf.pages[0].extract_text()
    assert "Room 4B" in pdf.pages[0].extract_text()

    data.update({"paper": "a4", "layout": "quick_action_cards",
                 "selection": "selected", "quick_ids": str(quick_id)})
    quick_pdf = _reader(client.post(f"{root}/print.pdf", data=data))
    assert "QUICK:QA1" in quick_pdf.pages[0].extract_text()
    assert "STUDENT:01" not in quick_pdf.pages[0].extract_text()

    data.update({"layout": "qr_only", "student_ids": str(student_id),
                 "action_ids": str(action_id)})
    mixed = _reader(client.post(f"{root}/print.pdf", data=data))
    content = mixed.pages[0].extract_text()
    assert all(code in content for code in ("STUDENT:01", "QUICK:QA1", "ACTION:A01"))

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FinancialTransaction).where(
            FinancialTransaction.classroom_id == classroom_id,
        )) == 0

    other, _ = _client(engine)
    assert other.post(f"{root}/print.pdf", data=data).status_code == 404
    guest = TestClient(app, follow_redirects=False)
    assert guest.post(f"{root}/print.pdf", data=data).status_code == 303
    data.update({"student_ids": str(uuid.uuid4())})
    assert client.post(f"{root}/print.pdf", data=data).status_code == 404
    data.update({"student_ids": str(student_id), "csrf_token": "wrong"})
    assert client.post(f"{root}/print.pdf", data=data).status_code in {400, 403}


def test_pdf_route_rejects_inactive_and_empty_selected_items(engine):
    client, owner = _client(engine)
    classroom_id, student_id, quick_id, _ = _setup(engine, owner)
    root = f"/classrooms/{classroom_id}/qr"
    client.get(root)
    data = {"csrf_token": client.cookies.get(settings.csrf_cookie_name),
            "paper": "letter", "layout": "student_cards", "selection": "selected"}
    assert client.post(f"{root}/print.pdf", data=data).status_code == 400
    with Session(engine) as db, db.begin():
        db.get(QuickAction, quick_id).active = False
    data.update({"layout": "quick_action_cards", "quick_ids": str(quick_id)})
    assert client.post(f"{root}/print.pdf", data=data).status_code == 404
    data.update({"layout": "student_cards", "student_ids": str(student_id),
                 "paper": "invalid"})
    assert client.post(f"{root}/print.pdf", data=data).status_code == 400
