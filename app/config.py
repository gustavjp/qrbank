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
    active_classroom_cookie_name: str
    session_ttl_seconds: int
    session_cookie_secure: bool
    csrf_cookie_name: str
    verification_token_ttl_seconds: int
    password_reset_token_ttl_seconds: int
    email_delivery_mode: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    smtp_from_address: str


def load_settings() -> Settings:
    environment = os.getenv("QRBANK_ENVIRONMENT", "development")
    smtp_security = os.getenv("QRBANK_SMTP_SECURITY", "starttls").strip().lower()
    return Settings(
        database_url=os.getenv(
            "QRBANK_DATABASE_URL",
            "postgresql+psycopg://qrbank:qrbank@localhost:5432/qrbank",
        ),
        environment=environment,
        public_base_url=os.getenv("QRBANK_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        session_cookie_name=os.getenv("QRBANK_SESSION_COOKIE_NAME", "qrbank_session"),
        active_classroom_cookie_name=os.getenv(
            "QRBANK_ACTIVE_CLASSROOM_COOKIE_NAME", "qrbank_active_classroom"
        ),
        session_ttl_seconds=int(os.getenv("QRBANK_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 14))),
        session_cookie_secure=_env_bool(
            "QRBANK_SESSION_COOKIE_SECURE", environment == "production"
        ),
        csrf_cookie_name=os.getenv("QRBANK_CSRF_COOKIE_NAME", "qrbank_csrf"),
        verification_token_ttl_seconds=int(
            os.getenv("QRBANK_VERIFICATION_TOKEN_TTL_SECONDS", str(60 * 60 * 24))
        ),
        password_reset_token_ttl_seconds=int(
            os.getenv("QRBANK_PASSWORD_RESET_TOKEN_TTL_SECONDS", str(60 * 30))
        ),
        email_delivery_mode=os.getenv("QRBANK_EMAIL_DELIVERY_MODE", "console").strip().lower(),
        smtp_host=os.getenv("QRBANK_SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("QRBANK_SMTP_PORT", "465" if smtp_security == "ssl" else "587")),
        smtp_security=smtp_security,
        smtp_username=os.getenv("QRBANK_SMTP_USERNAME", ""),
        smtp_password=os.getenv("QRBANK_SMTP_PASSWORD", ""),
        smtp_from_address=os.getenv("QRBANK_SMTP_FROM_ADDRESS", "").strip(),
    )


settings = load_settings()
