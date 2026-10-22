from __future__ import annotations

import csv
import re
import uuid
from dataclasses import dataclass
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

from app.services.qr_codes import FORMATS, qr_command, render_qr


MAX_ARCHIVE_ITEMS = 120
FOLDERS = {"student": "students", "quick": "quick_actions", "action": "actions"}


@dataclass(frozen=True)
class ArchiveQr:
    kind: str
    entity_id: uuid.UUID
    code: str
    label: str


def _safe_filename_code(code: str) -> str:
    # The UUID keeps names unique; this portion is only for human readability.
    return re.sub(r"[^a-z0-9_-]+", "-", code.lower()).strip("-")[:40] or "code"


def _csv_cell(value: str) -> str:
    # CSV quoting does not prevent spreadsheet formula execution.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def build_qr_archive(
    items: list[ArchiveQr], *, image_format: str,
    classroom_name: str, school_year: str,
) -> bytes:
    """Create a self-contained ZIP of scanner-ready images and a CSV index."""
    if image_format not in FORMATS:
        raise ValueError("unsupported QR image format")
    if not 1 <= len(items) <= MAX_ARCHIVE_ITEMS:
        raise ValueError("invalid number of QR images")

    manifest = StringIO(newline="")
    writer = csv.writer(manifest)
    writer.writerow(("file", "type", "code", "command", "label", "classroom", "school_year"))
    result = BytesIO()
    filenames: set[str] = set()

    with ZipFile(result, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for item in items:
            folder = FOLDERS.get(item.kind)
            if folder is None:
                raise ValueError("unknown QR entity type")
            command = qr_command(item.kind, item.code)
            filename = (
                f"{folder}/{_safe_filename_code(item.code)}-{item.entity_id.hex}.{image_format}"
            )
            if filename in filenames:
                raise ValueError("duplicate QR image")
            filenames.add(filename)
            archive.writestr(filename, render_qr(command, image_format))
            writer.writerow(tuple(_csv_cell(v) for v in (
                filename, item.kind, item.code, command, item.label,
                classroom_name, school_year,
            )))

        archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))
    return result.getvalue()
