"""Configuration checks do not require PostgreSQL or a Windows host."""

from pathlib import Path
from runpy import run_path


# The deployment utility is intentionally outside the installed `app` package.
validate_environment = run_path(
    str(Path(__file__).resolve().parents[1] / "deploy" / "check_environment.py")
)["validate_environment"]


def _development_env() -> dict[str, str]:
    return {
        "QRBANK_DATABASE_URL": "postgresql+psycopg://qrbank_app:good-secret@127.0.0.1:5432/qrbank",
        "QRBANK_ENVIRONMENT": "development",
        "QRBANK_PUBLIC_BASE_URL": "http://localhost:8000",
        "QRBANK_EMAIL_DELIVERY_MODE": "console",
    }


def test_development_preview_configuration_passes() -> None:
    assert validate_environment(_development_env()) == []


def test_missing_configuration_fails_closed() -> None:
    problems = validate_environment({})
    assert any("QRBANK_DATABASE_URL" in p for p in problems)
    assert any("QRBANK_ENVIRONMENT" in p for p in problems)
    assert any("QRBANK_PUBLIC_BASE_URL" in p for p in problems)


def test_demo_database_credentials_are_rejected() -> None:
    env = _development_env()
    env["QRBANK_DATABASE_URL"] = "postgresql+psycopg://qrbank:qrbank@localhost:5432/qrbank"
    assert any("demo database" in p for p in validate_environment(env))


def test_malformed_database_url_does_not_expose_password() -> None:
    env = _development_env()
    env["QRBANK_DATABASE_URL"] = "postgresql+psycopg://qrbank:very-private-secret@[::bad/qrbank"
    problems = validate_environment(env)
    assert any("valid URL" in p for p in problems)
    assert "very-private-secret" not in " ".join(problems)


def test_unimplemented_email_mode_is_rejected() -> None:
    env = _development_env()
    env["QRBANK_EMAIL_DELIVERY_MODE"] = "smtp"
    assert any("Only console" in p for p in validate_environment(env))


def test_production_is_blocked_until_email_provider_exists() -> None:
    env = _development_env()
    env.update({
        "QRBANK_ENVIRONMENT": "production",
        "QRBANK_PUBLIC_BASE_URL": "https://qrbank.example.org",
        "QRBANK_SESSION_COOKIE_SECURE": "true",
    })
    problems = validate_environment(env)
    assert any("Production is blocked" in p for p in problems)
    assert not any("HTTPS" in p for p in problems)


def test_production_rejects_http_and_insecure_cookie_override() -> None:
    env = _development_env()
    env.update({
        "QRBANK_ENVIRONMENT": "production",
        "QRBANK_SESSION_COOKIE_SECURE": "false",
    })
    problems = validate_environment(env)
    assert any("HTTPS" in p for p in problems)
    assert any("cookies" in p for p in problems)
