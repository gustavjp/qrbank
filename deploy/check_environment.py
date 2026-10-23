"""Fail-closed configuration checks for a QRBank deployment preview.

This script makes no network or database connections and never prints credentials.
Run from the repository root: python deploy/check_environment.py
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit


DEMO_DATABASE_URL = "postgresql+psycopg://qrbank:qrbank@localhost:5432/qrbank"


def validate_environment(env: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    database_url = env.get("QRBANK_DATABASE_URL", "").strip()
    if not database_url:
        problems.append("Set QRBANK_DATABASE_URL explicitly; the application has a demo default.")
    elif database_url == DEMO_DATABASE_URL:
        problems.append("Replace the documented demo database credentials before running.")
    else:
        try:
            parsed = urlsplit(database_url)
            if (
                parsed.scheme != "postgresql+psycopg"
                or not parsed.hostname
                or not parsed.path.strip("/")
                or not parsed.username
                or not parsed.password
            ):
                problems.append("QRBANK_DATABASE_URL must be a PostgreSQL psycopg URL with a database, user, and password.")
        except ValueError:
            problems.append("QRBANK_DATABASE_URL is not a valid URL; check encoding of special characters.")

    environment = env.get("QRBANK_ENVIRONMENT", "").strip()
    if environment not in {"development", "production"}:
        problems.append("Set QRBANK_ENVIRONMENT explicitly to development or production.")

    base_url = env.get("QRBANK_PUBLIC_BASE_URL", "").strip()
    if not base_url:
        problems.append("Set QRBANK_PUBLIC_BASE_URL explicitly.")
    else:
        parsed_base = urlsplit(base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            problems.append("QRBANK_PUBLIC_BASE_URL must be an absolute http(s) URL.")
        if environment == "production" and parsed_base.scheme != "https":
            problems.append("Production requires an HTTPS QRBANK_PUBLIC_BASE_URL.")

    email_mode = env.get("QRBANK_EMAIL_DELIVERY_MODE", "console").strip()
    if environment == "production":
        cookie_setting = env.get("QRBANK_SESSION_COOKIE_SECURE", "").strip().lower()
        if cookie_setting in {"0", "false", "no", "off"}:
            problems.append("Production session cookies cannot be marked insecure.")
        # As of this milestone, the application only has dev-console email delivery.
        problems.append("Production is blocked: real verification/reset email delivery is not implemented.")
    elif email_mode != "console":
        problems.append("Only console email delivery is currently implemented for development.")

    return problems


def main() -> int:
    problems = validate_environment(os.environ)
    if problems:
        print("QRBank preflight: NOT READY")
        for issue in problems:
            print(f"- {issue}")
        return 1

    print("QRBank preflight: development configuration checks passed.")
    print("This does not verify database connectivity or production readiness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
