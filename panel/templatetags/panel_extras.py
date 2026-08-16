from django import template
from django.conf import settings
from django.utils import timezone

from panel.services import storage_signed_url
from panel.value_utils import humanize_value

register = template.Library()


@register.filter
def initials(value):
    """Devuelve hasta dos iniciales legibles para avatares sin fotografía."""
    words = [part for part in str(value or "").strip().split() if part]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return f"{words[0][0]}{words[1][0]}".upper()


@register.filter
def comma_join(value):
    return humanize_value(value)


@register.filter
def clean_value(value):
    """Oculta sintaxis de JSON/arrays en valores heredados de la ficha médica."""
    return humanize_value(value)


@register.filter
def money(value):
    try:
        return f"${float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0,00"


@register.filter
def patient_photo(path):
    return storage_signed_url(path, settings.SUPABASE_PATIENT_BUCKET)


@register.filter
def payment_proof(path):
    return storage_signed_url(path, settings.SUPABASE_PAYMENT_BUCKET)


@register.filter
def profile_asset(path):
    return storage_signed_url(path, settings.SUPABASE_PROFILE_BUCKET)


@register.filter
def status_label(value):
    labels = {
        "pending": "Pendiente", "confirmed": "Confirmado", "approved": "Aprobado",
        "rejected": "Rechazado", "production": "En producción", "in_production": "En producción",
        "shipped": "Enviado", "delivered": "Entregado", "cancelled": "Cancelado",
        "active": "Activo", "inactive": "Inactivo",
    }
    return labels.get(str(value).lower(), str(value).replace("_", " ").title())


@register.filter
def status_step(value):
    return {"pending": 1, "confirmed": 1, "production": 2, "in_production": 2, "shipped": 3, "delivered": 4}.get(str(value).lower(), 1)


@register.filter
def payment_method_label(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"transfer", "transferencia", "bank_transfer", "transferencia_bancaria"}:
        return "Transferencia"
    if normalized in {"deposit", "deposito", "depósito", "bank_deposit"}:
        return "Depósito"
    return normalized.replace("_", " ").title() if normalized else "No definido"


@register.filter
def age(birth_date):
    if not birth_date:
        return "—"
    today = timezone.localdate()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
