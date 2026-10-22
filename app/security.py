from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash


_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> bytes:
    if not token:
        raise ValueError("session token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_one_time_token() -> str:
    return secrets.token_urlsafe(32)


def hash_one_time_token(token: str) -> bytes:
    if not token:
        raise ValueError("token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).digest()
