from __future__ import annotations

import logging
from urllib.parse import quote

from app.config import settings


logger = logging.getLogger("qrbank.email")


class EmailDeliveryNotConfigured(RuntimeError):
    pass


def build_verification_url(token: str) -> str:
    return f"{settings.public_base_url}/auth/verify-email?token={quote(token, safe='')}"


def send_verification_email(*, email: str, token: str) -> None:
    url = build_verification_url(token)

    if settings.email_delivery_mode == "console" and settings.environment != "production":
        logger.warning("DEV verification email for %s: %s", email, url)
        return

    raise EmailDeliveryNotConfigured(
        "production email delivery is not configured"
    )
