from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    environment: str
    public_base_url: str
    session_cookie_name: str
    session_ttl_seconds: int
    session_cookie_secure: bool
    csrf_cookie_name: str
    verification_token_ttl_seconds: int
    email_delivery_mode: str


def load_settings() -> Settings:
    environment = os.getenv("QRBANK_ENVIRONMENT", "development")
    return Settings(
        database_url=os.getenv(
            "QRBANK_DATABASE_URL",
            "postgresql+psycopg://qrbank:qrbank@localhost:5432/qrbank",
        ),
        environment=environment,
        public_base_url=os.getenv("QRBANK_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        session_cookie_name=os.getenv("QRBANK_SESSION_COOKIE_NAME", "qrbank_session"),
        session_ttl_seconds=int(os.getenv("QRBANK_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 14))),
        session_cookie_secure=_env_bool(
            "QRBANK_SESSION_COOKIE_SECURE",
            environment == "production",
        ),
        csrf_cookie_name=os.getenv("QRBANK_CSRF_COOKIE_NAME", "qrbank_csrf"),
        verification_token_ttl_seconds=int(
            os.getenv("QRBANK_VERIFICATION_TOKEN_TTL_SECONDS", str(60 * 60 * 24))
        ),
        email_delivery_mode=os.getenv("QRBANK_EMAIL_DELIVERY_MODE", "console"),
    )


settings = load_settings()
