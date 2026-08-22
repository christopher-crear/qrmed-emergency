"""Generación de facturas PDF para pedidos aprobados de QRMed."""
from __future__ import annotations

import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


BLUE = HexColor("#1762FF")
NAVY = HexColor("#07162D")
MUTED = HexColor("#6E829D")
LINE = HexColor("#DFE7F1")
SOFT = HexColor("#F4F7FB")
GREEN = HexColor("#079A60")


def _money(value):
    return f"${Decimal(value or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_invoice_pdf(*, invoice, order, items, products, customer, company):
    stream = io.BytesIO()
    doc = SimpleDocTemplate(
        stream, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Factura {invoice.invoice_number}", author="QRMed Emergency",
    )
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=13, textColor=NAVY)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=11, textColor=MUTED)
    heading = ParagraphStyle("heading", parent=body, fontName="Helvetica-Bold", fontSize=19, leading=23)
    right = ParagraphStyle("right", parent=body, alignment=2)
    right_small = ParagraphStyle("right-small", parent=small, alignment=2)
    story = []

    brand = Table([
        [Paragraph("<b>QRMed</b> <font color='#1762FF'>Emergency</font>", heading),
         Paragraph(f"<b>FACTURA</b><br/><font size='9'>{invoice.invoice_number}</font>", right)],
    ], colWidths=[98 * mm, 59 * mm])
    brand.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, BLUE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.extend([brand, Spacer(1, 7 * mm)])

    company_lines = [
        f"<b>{company.get('name', 'QRMed Emergency')}</b>",
        f"RUC/Identificación: {company.get('tax_id') or 'No registrado'}",
        company.get("address") or "Loja, Ecuador",
        f"Teléfono: {company.get('phone') or 'No registrado'}",
        f"Correo: {company.get('email') or 'No registrado'}",
    ]
    customer_name = getattr(customer, "full_name", None) or order.shipping_name or "Cliente QRMed"
    customer_id = getattr(customer, "id_number", None) or "No registrado"
    customer_email = getattr(customer, "email", None) or "No registrado"
    customer_phone = getattr(customer, "phone", None) or order.shipping_phone or "No registrado"
    issue_date = invoice.issued_at.strftime("%d/%m/%Y %H:%M") if invoice.issued_at else "-"
    info = Table([
        [Paragraph("<b>EMISOR</b><br/>" + "<br/>".join(company_lines), body),
         Paragraph(
             "<b>CLIENTE</b><br/>"
             f"{customer_name}<br/>Identificación: {customer_id}<br/>"
             f"Correo: {customer_email}<br/>Teléfono: {customer_phone}", body,
         )],
        [Paragraph(f"<b>Pedido:</b> {order.order_number}", body),
         Paragraph(f"<b>Fecha de emisión:</b> {issue_date}", body)],
    ], colWidths=[79 * mm, 79 * mm])
    info.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("BOX", (0, 0), (-1, -1), .6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), .4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 9),
    ]))
    story.extend([info, Spacer(1, 8 * mm)])

    rows = [["Producto", "Detalle", "Cant.", "P. unitario", "Total"]]
    calculated_subtotal = Decimal("0")
    for item in items:
        product = products.get(str(item.product_id))
        name = getattr(product, "name", None) or "Producto QRMed"
        detail = " / ".join(filter(None, [item.selected_color, f"Talla {item.selected_size}" if item.selected_size else ""])) or "-"
        line_total = Decimal(item.unit_price or 0) * int(item.quantity or 0)
        calculated_subtotal += line_total
        rows.append([
            Paragraph(str(name), body), Paragraph(detail, small), str(item.quantity),
            _money(item.unit_price), _money(line_total),
        ])
    product_table = Table(rows, colWidths=[56 * mm, 39 * mm, 16 * mm, 23 * mm, 24 * mm], repeatRows=1)
    product_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("GRID", (0, 0), (-1, -1), .45, LINE),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([product_table, Spacer(1, 5 * mm)])

    invoice_subtotal = Decimal(order.subtotal or 0) or calculated_subtotal
    totals = Table([
        ["Subtotal", _money(invoice_subtotal)],
        [f"Descuento {order.discount_code or ''}".strip(), f"-{_money(order.discount_amount)}"],
        ["TOTAL", _money(order.total)],
    ], colWidths=[42 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, 1), GREEN),
        ("TEXTCOLOR", (0, 2), (-1, 2), NAVY),
        ("BACKGROUND", (0, 2), (-1, 2), SOFT),
        ("LINEABOVE", (0, 2), (-1, 2), .8, LINE),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([totals, Spacer(1, 8 * mm)])

    delivery = Table([[
        Paragraph("<b>CÓDIGO DE ENTREGA</b><br/><font size='20' color='#1762FF'>"
                  f"{order.tracking_number or '-'}</font>", body),
        Paragraph("Entrega este código al motorizado únicamente después de recibir y revisar tu pedido.", small),
    ]], colWidths=[62 * mm, 96 * mm])
    delivery.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#EDF4FF")),
        ("BOX", (0, 0), (-1, -1), .8, HexColor("#C9DAFF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 10),
    ]))
    story.extend([delivery, Spacer(1, 8 * mm), Paragraph(
        "Documento generado electrónicamente por QRMed Emergency. Conserva esta factura como respaldo de tu compra.",
        right_small,
    )])

    doc.build(story)
    stream.seek(0)
    return stream.getvalue()
