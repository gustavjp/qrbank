from __future__ import annotations


def safe_spreadsheet_text(value: str | None) -> str:
    """Prevent spreadsheet formula evaluation in otherwise untrusted CSV text."""
    if value is None:
        return ""
    if value.startswith(("\t", "\r", "\n")) or value.lstrip(
        " \t\r\n\ufeff\u200b"
    ).startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
