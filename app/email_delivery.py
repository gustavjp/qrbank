from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from app.config import settings


logger = logging.getLogger("qrbank.email")


class EmailDeliveryError(RuntimeError):
    """Delivery could not be completed; avoid displaying technical details."""


class EmailDeliveryNotConfigured(EmailDeliveryError):
    pass


class EmailDeliveryFailed(EmailDeliveryError):
    pass


def build_verification_url(token: str) -> str:
    return f"{settings.public_base_url}/auth/verify-email?token={quote(token, safe='')}"


def build_password_reset_url(token: str) -> str:
    return f"{settings.public_base_url}/auth/reset-password?token={quote(token, safe='')}"


def _smtp_is_configured() -> bool:
    return bool(
        settings.smtp_host
        and 1 <= settings.smtp_port <= 65535
        and settings.smtp_security in {"starttls", "ssl"}
        and settings.smtp_username.strip()
        and settings.smtp_password
        and settings.smtp_from_address
        and "\r" not in settings.smtp_from_address
        and "\n" not in settings.smtp_from_address
        and "@" in settings.smtp_from_address
        and (
            settings.environment != "production"
            or settings.public_base_url.startswith("https://")
        )
    )


def email_delivery_is_available() -> bool:
    if settings.email_delivery_mode == "console":
        return settings.environment != "production"
    if settings.email_delivery_mode == "smtp":
        return _smtp_is_configured()
    return False


def _send_smtp_email(*, email: str, subject: str, body: str) -> None:
    if not _smtp_is_configured():
        raise EmailDeliveryNotConfigured("SMTP email delivery is not configured")

    try:
        message = EmailMessage()
        message["From"] = settings.smtp_from_address
        message["To"] = email
        message["Subject"] = subject
        message.set_content(body)

        if settings.smtp_security == "ssl":
            connection = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=10,
                context=ssl.create_default_context(),
            )
        else:
            connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)

        with connection as smtp:
            if settings.smtp_security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        # Never log the message body, token-bearing link, or SMTP password.
        logger.error("SMTP message delivery failed (%s)", type(exc).__name__)
        raise EmailDeliveryFailed("email delivery failed") from None


def send_verification_email(*, email: str, token: str) -> None:
    if not email_delivery_is_available():
        raise EmailDeliveryNotConfigured("email delivery is not configured")

    url = build_verification_url(token)
    if settings.email_delivery_mode == "console":
        logger.warning("DEV verification email for %s: %s", email, url)
        return

    _send_smtp_email(
        email=email,
        subject="Verify your QRBank email address",
        body=(
            "Finish creating your QRBank account by opening this link:\n\n"
            f"{url}\n\n"
            "If you did not create a QRBank account, you can ignore this email."
        ),
    )


def send_password_reset_email(*, email: str, token: str) -> None:
    if not email_delivery_is_available():
        raise EmailDeliveryNotConfigured("email delivery is not configured")

    url = build_password_reset_url(token)
    if settings.email_delivery_mode == "console":
        logger.warning("DEV password reset email for %s: %s", email, url)
        return

    _send_smtp_email(
        email=email,
        subject="Reset your QRBank password",
        body=(
            "Someone requested a password reset for your QRBank account.\n\n"
            f"{url}\n\n"
            "This link can be used once and expires automatically. "
            "If you did not request it, you can ignore this email."
        ),
    )
