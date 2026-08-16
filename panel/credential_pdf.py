"""Generador PDF de la credencial médica QRMed.

El PDF contiene el frente y reverso de una credencial vertical inspirada en
la referencia visual del proyecto. Se genera completamente del lado del
servidor para que el usuario descargue un archivo consistente en cualquier
navegador.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Optional

from PIL import Image, ImageDraw, ImageOps
from reportlab.graphics.barcode import code128
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .value_utils import humanize_value


NAVY = HexColor("#0B2A57")
NAVY_DARK = HexColor("#071A38")
BLUE = HexColor("#1762FF")
CYAN = HexColor("#20C8E8")
MINT = HexColor("#DDFBF4")
MINT_STRONG = HexColor("#63E6CF")
TEXT_SOFT = HexColor("#DDEAFF")
PAGE_BG = HexColor("#F4F7FB")
RED = HexColor("#EF3340")
GREEN = HexColor("#15B981")
WHITE_80 = Color(1, 1, 1, alpha=0.80)

CARD_W = 248
CARD_H = 392
CARD_RADIUS = 11


def _safe_text(value, default="—"):
    return humanize_value(value, default=default)


def _date_text(value):
    if not value:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return _safe_text(value)


def _fit_text(text, font="Helvetica-Bold", max_size=18, min_size=8, max_width=180):
    text = _safe_text(text)
    size = float(max_size)
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 0.5
    if stringWidth(text, font, size) <= max_width:
        return text, size
    shortened = text
    while len(shortened) > 2 and stringWidth(shortened + "…", font, size) > max_width:
        shortened = shortened[:-1]
    return shortened.rstrip() + "…", size


def _initials(patient):
    first = _safe_text(getattr(patient, "first_name", ""), "")
    last = _safe_text(getattr(patient, "last_name", ""), "")
    letters = "".join(part[:1] for part in (first, last) if part)
    return (letters or "QR")[:2].upper()


def _circular_photo(photo_bytes: bytes, size: int = 520) -> Optional[io.BytesIO]:
    if not photo_bytes:
        return None
    try:
        source = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
        source = ImageOps.fit(source, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size - 1, size - 1), fill=255)
        out = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        out.paste(source, (0, 0), mask)
        stream = io.BytesIO()
        out.save(stream, format="PNG", optimize=True)
        stream.seek(0)
        return stream
    except Exception:
        return None


def _draw_brand(c: canvas.Canvas, x, y, dark=False):
    brand_color = white if dark else NAVY
    c.setStrokeColor(CYAN if dark else BLUE)
    c.setLineWidth(1.6)
    c.circle(x + 12, y + 10, 10, stroke=1, fill=0)
    path = c.beginPath()
    path.moveTo(x + 4, y + 10)
    path.lineTo(x + 8, y + 10)
    path.lineTo(x + 10, y + 16)
    path.lineTo(x + 13, y + 4)
    path.lineTo(x + 16, y + 12)
    path.lineTo(x + 21, y + 12)
    c.drawPath(path, stroke=1, fill=0)
    c.setFillColor(brand_color)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(x + 29, y + 4, "QRMed")
    c.setFont("Helvetica", 10.2)
    c.drawString(x + 81, y + 5, "Emergency")


def _draw_heartbeat(c, x, y, width, color=MINT_STRONG, line_width=7):
    c.setStrokeColor(color)
    c.setLineWidth(line_width)
    c.setLineJoin(1)
    c.setLineCap(1)
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x + width * 0.12, y)
    p.lineTo(x + width * 0.17, y + 46)
    p.lineTo(x + width * 0.22, y - 31)
    p.lineTo(x + width * 0.31, y + 9)
    p.lineTo(x + width * 0.37, y)
    p.lineTo(x + width, y)
    c.drawPath(p, stroke=1, fill=0)


def _draw_blue_background(c, x, y, w, h):
    c.saveState()
    try:
        c.setFillAlpha(1)
        c.setStrokeAlpha(1)
    except AttributeError:
        pass
    path = c.beginPath()
    path.roundRect(x, y, w, h, CARD_RADIUS)
    c.clipPath(path, stroke=0, fill=0)
    try:
        c.linearGradient(
            x, y, x + w, y + h,
            [NAVY_DARK, NAVY, BLUE],
            positions=[0, 0.56, 1],
            extend=True,
        )
    except Exception:
        c.setFillColor(NAVY)
        c.rect(x, y, w, h, stroke=0, fill=1)
    # Motivo médico sutil en el fondo.
    c.setFillColor(Color(1, 1, 1, alpha=0.055))
    for dx, dy, s in ((26, 48, 16), (63, 83, 12), (182, 52, 18), (209, 112, 11), (31, 184, 9)):
        c.rect(x + dx - s / 4, y + dy - s / 2, s / 2, s, stroke=0, fill=1)
        c.rect(x + dx - s / 2, y + dy - s / 4, s, s / 2, stroke=0, fill=1)
    c.restoreState()


def _draw_front(c, x, y, patient, photo_bytes=None):
    # Sombra y tarjeta.
    c.setFillColor(Color(0.02, 0.08, 0.18, alpha=0.16))
    c.roundRect(x + 5, y - 5, CARD_W, CARD_H, CARD_RADIUS, stroke=0, fill=1)
    c.setFillColor(MINT)
    c.roundRect(x, y, CARD_W, CARD_H, CARD_RADIUS, stroke=0, fill=1)

    # Sección inferior azul.
    lower_h = 145
    c.saveState()
    clip = c.beginPath()
    clip.roundRect(x, y, CARD_W, CARD_H, CARD_RADIUS)
    c.clipPath(clip, stroke=0, fill=0)
    _draw_blue_background(c, x, y, CARD_W, lower_h + 8)
    c.restoreState()

    _draw_brand(c, x + 31, y + CARD_H - 47, dark=False)

    # Foto circular.
    photo_size = 145
    photo_x = x + (CARD_W - photo_size) / 2
    photo_y = y + 158
    c.setFillColor(white)
    c.circle(photo_x + photo_size / 2, photo_y + photo_size / 2, photo_size / 2 + 5, stroke=0, fill=1)
    c.setStrokeColor(CYAN)
    c.setLineWidth(4)
    c.circle(photo_x + photo_size / 2, photo_y + photo_size / 2, photo_size / 2 + 1, stroke=1, fill=0)

    circular = _circular_photo(photo_bytes) if photo_bytes else None
    if circular:
        c.drawImage(ImageReader(circular), photo_x, photo_y, width=photo_size, height=photo_size, mask="auto")
    else:
        c.setFillColor(HexColor("#EAF5F8"))
        c.circle(photo_x + photo_size / 2, photo_y + photo_size / 2, photo_size / 2 - 2, stroke=0, fill=1)
        c.setFillColor(BLUE)
        c.setFont("Helvetica-Bold", 42)
        initials = _initials(patient)
        c.drawCentredString(photo_x + photo_size / 2, photo_y + photo_size / 2 - 14, initials)

    # Pulso cruzando la transición de color.
    _draw_heartbeat(c, x + 5, y + 150, CARD_W - 10, MINT_STRONG, 8)

    name, name_size = _fit_text(getattr(patient, "full_name", ""), max_size=20, min_size=11, max_width=CARD_W - 30)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", name_size)
    c.drawCentredString(x + CARD_W / 2, y + 92, name)
    c.setFont("Helvetica", 14)
    c.drawCentredString(x + CARD_W / 2, y + 68, "Paciente")

    blood = _safe_text(getattr(patient, "blood_type", ""), "S/D")
    c.setFillColor(RED)
    c.roundRect(x + 25, y + 24, 48, 24, 8, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(x + 49, y + 32, blood)

    # Código de barras real basado en la identificación.
    barcode_value = _safe_text(getattr(patient, "id_number", ""), "QRMed")
    try:
        barcode = code128.Code128(barcode_value, barHeight=19, barWidth=0.62, humanReadable=False)
        scale = min(1.0, 137 / max(barcode.width, 1))
        c.saveState()
        c.translate(x + 89, y + 21)
        c.scale(scale, 1)
        barcode.drawOn(c, 0, 0)
        c.restoreState()
    except Exception:
        pass


def _draw_back(c, x, y, patient, qr_bytes):
    c.setFillColor(Color(0.02, 0.08, 0.18, alpha=0.16))
    c.roundRect(x + 5, y - 5, CARD_W, CARD_H, CARD_RADIUS, stroke=0, fill=1)
    _draw_blue_background(c, x, y, CARD_W, CARD_H)

    # Cabecera clara y pulso.
    c.saveState()
    clip = c.beginPath()
    clip.roundRect(x, y, CARD_W, CARD_H, CARD_RADIUS)
    c.clipPath(clip, stroke=0, fill=0)
    c.setFillColor(MINT)
    c.rect(x, y + CARD_H - 88, CARD_W, 100, stroke=0, fill=1)
    c.restoreState()
    _draw_brand(c, x + 31, y + CARD_H - 47, dark=False)
    _draw_heartbeat(c, x + 5, y + CARD_H - 101, CARD_W - 10, MINT_STRONG, 8)

    name, name_size = _fit_text(getattr(patient, "full_name", ""), max_size=18, min_size=10, max_width=CARD_W - 30)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", name_size)
    c.drawCentredString(x + CARD_W / 2, y + 277, name)
    c.setFont("Helvetica", 9.5)
    c.setFillColor(TEXT_SOFT)
    c.drawCentredString(x + CARD_W / 2, y + 260, f"ID. {_safe_text(getattr(patient, 'id_number', ''))}")

    # QR principal.
    qr_size = 120
    qr_x = x + (CARD_W - qr_size) / 2
    qr_y = y + 126
    c.setFillColor(white)
    c.roundRect(qr_x - 7, qr_y - 7, qr_size + 14, qr_size + 14, 8, stroke=0, fill=1)
    c.drawImage(ImageReader(io.BytesIO(qr_bytes)), qr_x, qr_y, qr_size, qr_size, mask="auto")

    c.setFillColor(TEXT_SOFT)
    c.setFont("Helvetica", 7.2)
    c.drawCentredString(x + CARD_W / 2, y + 111, "Escanea para consultar la ficha médica de emergencia")

    issue_date = _date_text(getattr(patient, "created_at", None))
    status = str(getattr(patient, "status", "active") or "active").lower()
    status_label = "ACTIVA" if status == "active" else "INACTIVA"
    status_color = GREEN if status == "active" else RED

    c.setFillColor(WHITE_80)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + 21, y + 84, "Emisión")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8.3)
    c.drawString(x + 21, y + 72, issue_date)

    c.setFillColor(WHITE_80)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + 120, y + 84, "Tipo de sangre")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8.3)
    c.drawString(x + 120, y + 72, _safe_text(getattr(patient, "blood_type", ""), "S/D"))

    emergency_name = _safe_text(getattr(patient, "emergency_name", ""), "Sin registrar")
    emergency_phone = _safe_text(getattr(patient, "emergency_phone", ""), "—")
    emergency, emergency_size = _fit_text(
        f"{emergency_name} · {emergency_phone}",
        font="Helvetica-Bold", max_size=8.3, min_size=6.5, max_width=150,
    )
    c.setFillColor(WHITE_80)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + 21, y + 51, "Contacto de emergencia")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", emergency_size)
    c.drawString(x + 21, y + 38, emergency)

    c.setFillColor(status_color)
    c.roundRect(x + CARD_W - 66, y + 31, 45, 20, 7, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawCentredString(x + CARD_W - 43.5, y + 38, status_label)

    c.setFillColor(Color(1, 1, 1, alpha=0.55))
    c.setFont("Helvetica", 5.7)
    c.drawCentredString(x + CARD_W / 2, y + 14, "QRMed Emergency · Identificación médica digital")


def build_credential_pdf(patient, qr_bytes: bytes, photo_bytes: Optional[bytes] = None) -> bytes:
    """Devuelve el PDF final como bytes."""
    output = io.BytesIO()
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(output, pagesize=(page_w, page_h), pageCompression=1)
    c.setTitle(f"Credencial médica QRMed - {_safe_text(getattr(patient, 'full_name', 'Paciente'))}")
    c.setAuthor("QRMed Emergency")

    c.setFillColor(PAGE_BG)
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

    c.setFillColor(NAVY_DARK)
    c.setFont("Helvetica-Bold", 19)
    c.drawCentredString(page_w / 2, page_h - 45, "Credencial Médica QRMed Emergency")
    c.setFillColor(HexColor("#6A7E97"))
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(page_w / 2, page_h - 61, "Frente y reverso · Imprimir al 100% para conservar la proporción")

    gap = 48
    total_w = CARD_W * 2 + gap
    start_x = (page_w - total_w) / 2
    card_y = (page_h - CARD_H) / 2 - 10

    c.setFillColor(HexColor("#7388A3"))
    c.setFont("Helvetica-Bold", 7.8)
    c.drawString(start_x, card_y + CARD_H + 10, "FRENTE")
    c.drawString(start_x + CARD_W + gap, card_y + CARD_H + 10, "REVERSO")

    _draw_front(c, start_x, card_y, patient, photo_bytes=photo_bytes)
    _draw_back(c, start_x + CARD_W + gap, card_y, patient, qr_bytes=qr_bytes)

    c.setFillColor(HexColor("#8294AA"))
    c.setFont("Helvetica", 7)
    c.drawCentredString(
        page_w / 2, 18,
        "La información de esta credencial corresponde a la ficha médica registrada por el paciente.",
    )
    c.showPage()
    c.save()
    return output.getvalue()
