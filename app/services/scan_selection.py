from __future__ import annotations

import hashlib
import hmac
import time
import uuid


class SelectionExpiredError(ValueError):
    pass


def _signature(
    session_token: str, classroom_id: uuid.UUID,
    enrollment_id: uuid.UUID, issued_at: int,
) -> str:
    message = f"qrbank-select-v1:{classroom_id}:{enrollment_id}:{issued_at}".encode("utf-8")
    return hmac.new(session_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def issue_selection_token(
    *, session_token: str, classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
    now: int | None = None,
) -> str:
    """Bind a selected enrollment to this HttpOnly session and classroom."""
    if not session_token:
        raise SelectionExpiredError("sign in and select a student again")
    issued = int(time.time()) if now is None else now
    return f"{issued}.{_signature(session_token, classroom_id, enrollment_id, issued)}"


def verify_selection_token(
    *, session_token: str, classroom_id: uuid.UUID, enrollment_id: uuid.UUID,
    selection_token: str | None, timeout_seconds: int, now: int | None = None,
) -> int:
    """Return expiry time or reject an expired/tampered selection.

    Validation occurs on the server for every financial scan/confirmation; the
    browser countdown is informational and cannot extend the selection.
    """
    error = SelectionExpiredError("student selection expired; scan the student again")
    if not session_token or not selection_token or len(selection_token) > 128 or timeout_seconds <= 0:
        raise error
    try:
        stamp, signature = selection_token.split(".", 1)
        if not stamp.isascii() or not stamp.isdecimal() or len(stamp) > 12:
            raise ValueError("invalid timestamp")
        issued = int(stamp)
    except ValueError as exc:
        raise error from exc
    current = int(time.time()) if now is None else now
    if not 0 <= current - issued < timeout_seconds:
        raise error
    expected = _signature(session_token, classroom_id, enrollment_id, issued)
    if not hmac.compare_digest(signature, expected):
        raise error
    return issued + timeout_seconds
