from app.config import load_settings


def test_production_defaults_to_secure_session_cookie(monkeypatch) -> None:
    monkeypatch.setenv("QRBANK_ENVIRONMENT", "production")
    monkeypatch.delenv("QRBANK_SESSION_COOKIE_SECURE", raising=False)

    production = load_settings()

    assert production.session_cookie_secure is True


def test_development_defaults_to_nonsecure_cookie_for_local_http(monkeypatch) -> None:
    monkeypatch.setenv("QRBANK_ENVIRONMENT", "development")
    monkeypatch.delenv("QRBANK_SESSION_COOKIE_SECURE", raising=False)

    development = load_settings()

    assert development.session_cookie_secure is False
