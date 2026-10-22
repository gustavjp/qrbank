from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import segno
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from app.services.qr_codes import qr_command


PAPERS = {"letter": letter, "a4": A4}
LAYOUTS = {"qr_only", "student_cards", "quick_action_cards"}
MAX_PRINT_ITEMS = 120


@dataclass(frozen=True)
class PrintableQr:
    kind: str
    code: str
    label: str


def _fit(value: str, width: float, *, font: str, size: float) -> str:
    cleaned = " ".join(value.split())
    if pdfmetrics.stringWidth(cleaned, font, size) <= width:
        return cleaned
    while cleaned and pdfmetrics.stringWidth(cleaned + "...", font, size) > width:
        cleaned = cleaned[:-1]
    return cleaned.rstrip() + "..."


def _vector_qr(pdf: canvas.Canvas, payload: str, *, x: float, y: float, side: float) -> None:
    """Draw genuine vector QR modules, including four blank modules on each edge."""
    qr = segno.make_qr(payload, error="m", boost_error=False)
    modules = list(qr.matrix_iter(scale=1, border=4))
    width = len(modules)
    pitch = side / width
    pdf.setFillColor(colors.white)
    pdf.rect(x, y, side, side, fill=1, stroke=0)
    pdf.setFillColor(colors.black)
    for row, pixels in enumerate(modules):
        for col, dark in enumerate(pixels):
            if dark:
                pdf.rect(x + col * pitch, y + (width - row - 1) * pitch,
                         pitch + 0.02, pitch + 0.02, fill=1, stroke=0)


def generate_printable_pdf(
    items: list[PrintableQr], *, classroom_name: str, school_year: str,
    paper: str, layout: str,
) -> bytes:
    if paper not in PAPERS or layout not in LAYOUTS:
        raise ValueError("unsupported QR paper size or layout")
    if not items or len(items) > MAX_PRINT_ITEMS:
        raise ValueError(f"select between 1 and {MAX_PRINT_ITEMS} QR codes")
    if layout == "student_cards" and any(item.kind != "student" for item in items):
        raise ValueError("student card layout only accepts students")
    if layout == "quick_action_cards" and any(item.kind != "quick" for item in items):
        raise ValueError("Quick Action card layout only accepts Quick Actions")

    # Validate every command before writing output.
    commands = [qr_command(item.kind, item.code) for item in items]
    page_w, page_h = PAPERS[paper]
    columns, rows = (3, 3) if layout == "qr_only" else (2, 3)
    per_page = columns * rows
    margin = 36
    top = page_h - 105
    bottom = 54
    cell_w = (page_w - 2 * margin) / columns
    cell_h = (top - bottom) / rows
    title = {
        "qr_only": "QR Code Sheet",
        "student_cards": "Student QR Cards",
        "quick_action_cards": "Quick Action QR Cards",
    }[layout]
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(page_w, page_h), pageCompression=1)
    pdf.setTitle(f"QRBank - {title}")

    for start in range(0, len(items), per_page):
        page_number = start // per_page + 1
        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 17)
        pdf.drawString(margin, page_h - 43, title)
        pdf.setFont("Helvetica", 10)
        pdf.drawString(margin, page_h - 62,
                       _fit(f"Classroom: {classroom_name} ({school_year})",
                            page_w - 2 * margin, font="Helvetica", size=10))
        pdf.setFillColor(colors.HexColor("#404040"))
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(margin, page_h - 79,
                       "Scan in the correct classroom. Keep the full white border around every QR code.")

        for index, item in enumerate(items[start:start + per_page]):
            column = index % columns
            row = index // columns
            left = margin + column * cell_w
            base = top - (row + 1) * cell_h

            pdf.setStrokeColor(colors.HexColor("#bbbbbb"))
            pdf.setLineWidth(0.55)
            pdf.rect(left + 5, base + 5, cell_w - 10, cell_h - 10,
                     fill=0, stroke=1)
            pdf.setFillColor(colors.black)
            pdf.setFont("Helvetica-Bold", 11 if columns == 2 else 9)
            pdf.drawCentredString(left + cell_w / 2, base + cell_h - 23,
                                  _fit(item.label, cell_w - 22,
                                       font="Helvetica-Bold", size=11 if columns == 2 else 9))
            pdf.setFont("Helvetica", 8)
            pdf.drawCentredString(left + cell_w / 2, base + cell_h - 38,
                                  _fit(classroom_name, cell_w - 26,
                                       font="Helvetica", size=8))

            side = min(116 if columns == 3 else 136,
                       cell_w - 38, cell_h - 82)
            _vector_qr(pdf, commands[start + index],
                       x=left + (cell_w - side) / 2,
                       y=base + 38 + (cell_h - 82 - side) / 2, side=side)
            pdf.setFont("Courier", 8.5)
            pdf.drawCentredString(left + cell_w / 2, base + 21,
                                  commands[start + index])

        pdf.setFillColor(colors.HexColor("#555555"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margin, 34, "QRBank - Classroom-contextual commands")
        pdf.drawRightString(page_w - margin, 34, f"Page {page_number}")
        pdf.showPage()

    pdf.save()
    return output.getvalue()
