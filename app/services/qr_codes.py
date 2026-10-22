from __future__ import annotations

from io import BytesIO

import segno

from app.services.scanner import parse_scan_command


KINDS = {"student": "STUDENT", "quick": "QUICK", "action": "ACTION"}
FORMATS = {"png": "image/png", "svg": "image/svg+xml"}


def qr_command(kind: str, code: str) -> str:
    """Encode a public classroom-contextual scanner command, not a credential."""
    command_name = KINDS.get(kind)
    if command_name is None:
        raise ValueError("unknown QR entity type")
    parsed_kind, parsed_code = parse_scan_command(f"{command_name}:{code}")
    return f"{parsed_kind}:{parsed_code}"


def render_qr(payload: str, image_format: str) -> bytes:
    """Generate high-contrast QR images locally with a four-module quiet zone."""
    if image_format not in FORMATS:
        raise ValueError("unsupported QR image format")
    # Always use a regular QR Code, so USB and camera scanners can read it.
    qr = segno.make_qr(payload, error="m", boost_error=False)
    result = BytesIO()
    qr.save(
        result, kind=image_format,
        scale=12 if image_format == "png" else 8,
        border=4, dark="#000000", light="#ffffff",
    )
    return result.getvalue()
