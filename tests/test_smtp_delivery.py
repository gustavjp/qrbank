"""Real SMTP is mocked: tests never send network email."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import load_settings
from app.email_delivery import (
    EmailDeliveryFailed,
    EmailDeliveryNotConfigured,
    email_delivery_is_available,
    send_password_reset_email,
    send_verification_email,
)


def _settings(**overrides):
    values = {
        "environment": "production",
        "public_base_url": "https://bank.example.test",
        "email_delivery_mode": "smtp",
        "smtp_host": "mail.example.test",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "smtp-user",
        "smtp_password": "very-secret-smtp-password",
        "smtp_from_address": "notifications@example.test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeSMTP:
    def __init__(self):
        self.events = []
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, *, context):
        self.events.append("starttls")

    def login(self, username, password):
        self.events.append("login")
        assert username == "smtp-user"
        assert password == "very-secret-smtp-password"

    def send_message(self, message):
        self.events.append("send")
        self.message = message


class SMTPDeliveryTests(unittest.TestCase):
    def test_smtp_starttls_is_required_before_login(self):
        fake = _FakeSMTP()
        with patch("app.email_delivery.settings", _settings()), patch(
            "app.email_delivery.smtplib.SMTP", return_value=fake
        ) as connection:
            self.assertTrue(email_delivery_is_available())
            send_verification_email(email="teacher@example.test", token="verification-token")

        connection.assert_called_once_with("mail.example.test", 587, timeout=10)
        self.assertEqual(fake.events, ["ehlo", "starttls", "ehlo", "login", "send"])
        self.assertEqual(fake.message["Subject"], "Verify your QRBank email address")
        self.assertIn("https://bank.example.test/auth/verify-email?token=verification-token", fake.message.get_content())

    def test_ssl_connection_is_supported(self):
        fake = _FakeSMTP()
        with patch("app.email_delivery.settings", _settings(smtp_security="ssl", smtp_port=465)), patch(
            "app.email_delivery.smtplib.SMTP_SSL", return_value=fake
        ) as connection:
            send_password_reset_email(email="teacher@example.test", token="reset+token")

        self.assertEqual(connection.call_args.args, ("mail.example.test", 465))
        self.assertEqual(fake.events, ["login", "send"])
        self.assertIn("reset%2Btoken", fake.message.get_content())

    def test_production_console_mode_is_disabled(self):
        with patch("app.email_delivery.settings", _settings(email_delivery_mode="console")):
            self.assertFalse(email_delivery_is_available())
            with self.assertRaises(EmailDeliveryNotConfigured):
                send_password_reset_email(email="teacher@example.test", token="sensitive-token")

    def test_production_smtp_requires_https_and_auth(self):
        for broken in (
            {"public_base_url": "http://bank.example.test"},
            {"smtp_password": ""},
            {"smtp_security": "none"},
            {"smtp_host": ""},
        ):
            with self.subTest(broken=broken), patch("app.email_delivery.settings", _settings(**broken)):
                self.assertFalse(email_delivery_is_available())

    def test_transport_failure_does_not_log_token_or_password(self):
        with patch("app.email_delivery.settings", _settings()), patch(
            "app.email_delivery.smtplib.SMTP", side_effect=OSError("connection refused")
        ), patch("app.email_delivery.logger.error") as logged_error:
            with self.assertRaises(EmailDeliveryFailed):
                send_password_reset_email(email="teacher@example.test", token="sensitive-token")

        logged_error.assert_called_once()
        text = " ".join(str(value) for value in logged_error.call_args.args)
        self.assertNotIn("sensitive-token", text)
        self.assertNotIn("very-secret-smtp-password", text)

    def test_malformed_recipient_header_fails_before_network_send(self):
        with patch("app.email_delivery.settings", _settings()), patch(
            "app.email_delivery.smtplib.SMTP"
        ) as connection, patch("app.email_delivery.logger.error") as logged_error:
            with self.assertRaises(EmailDeliveryFailed):
                send_password_reset_email(
                    email="teacher@example.test\r\nBcc: other@example.test",
                    token="sensitive-token",
                )

        connection.assert_not_called()
        logged_error.assert_called_once()
        self.assertNotIn("sensitive-token", str(logged_error.call_args))

    def test_environment_defaults_to_starttls_and_587(self):
        with patch.dict("os.environ", {"QRBANK_SMTP_SECURITY": "starttls"}, clear=True):
            config = load_settings()
        self.assertEqual(config.smtp_port, 587)
        self.assertEqual(config.smtp_security, "starttls")

    def test_environment_ssl_defaults_to_465(self):
        with patch.dict("os.environ", {"QRBANK_SMTP_SECURITY": "ssl"}, clear=True):
            config = load_settings()
        self.assertEqual(config.smtp_port, 465)


if __name__ == "__main__":
    unittest.main()
