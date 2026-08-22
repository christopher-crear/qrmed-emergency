from django.conf import settings
from django.urls import reverse

from django.db import DatabaseError
from django.db.models import Q

from .models import DiscountTicket, Order, Patient, Profile
from .services import versioned_media_url


def admin_context(request):
    profile = (
        getattr(request, "admin_profile", None)
        or getattr(request, "account_profile", None)
        or getattr(request, "user_profile", None)
        or getattr(request, "_qrmed_session_profile", None)
    )
    if not profile and request.session.get("supabase_user_id"):
        try:
            profile = Profile.objects.get(id=request.session["supabase_user_id"])
        except (Profile.DoesNotExist, ValueError):
            profile = None
    role = (profile.role or "").lower() if profile else ""
    is_admin = role in {"admin", "administrador"}
    patient = getattr(request, "patient", None)
    if profile and not is_admin and patient is None:
        patient = Patient.objects.filter(Q(owner_id=profile.id) | Q(id=profile.id)).order_by("-created_at").first()
    avatar_url = ""
    if profile and profile.avatar_path:
        avatar_url = versioned_media_url(
            reverse("profile_avatar_image", kwargs={"profile_id": profile.id}),
            profile.updated_at,
        )
    elif patient and patient.photo_path:
        avatar_url = versioned_media_url(
            reverse("patient_photo_image", kwargs={"patient_id": patient.id}),
            patient.updated_at,
        )
    cart_data = request.session.get("patient_cart", {})
    cart_count = 0
    if isinstance(cart_data, dict):
        for item in cart_data.values():
            try:
                cart_count += max(1, int(item.get("quantity", 1)))
            except (AttributeError, TypeError, ValueError):
                cart_count += 1
    preferences = profile.preferences if profile and isinstance(profile.preferences, dict) else {}
    notifications = []
    needs_medical_profile = bool(preferences.get("medical_profile_pending"))
    ticket_count = 0
    if profile and not is_admin:
        try:
            ticket_count = DiscountTicket.objects.filter(user_id=profile.id, used_at__isnull=True).count()
        except DatabaseError:
            ticket_count = 0
    try:
        if profile and is_admin:
            if preferences.get("system_alerts", True):
                pending_payments = Order.objects.exclude(payment_proof_path__isnull=True).exclude(
                    payment_proof_path=""
                ).filter(payment_reviewed_at__isnull=True).filter(
                    Q(payment_rejection_reason__isnull=True) | Q(payment_rejection_reason="")
                )[:5]
                for order in pending_payments:
                    notifications.append({
                        "title": "Pago pendiente de verificación",
                        "text": order.order_number,
                        "url": reverse("payment_detail", kwargs={"order_id": order.id}),
                        "icon": "receipt-text",
                    })
            if preferences.get("order_updates", True):
                pending_orders = Order.objects.filter(status__in=["pending", "confirmed"])[:5]
                for order in pending_orders:
                    notifications.append({
                        "title": "Nuevo pedido" if order.status == "pending" else "Pedido confirmado",
                        "text": order.order_number,
                        "url": reverse("order_detail", kwargs={"order_id": order.id}),
                        "icon": "package",
                    })
        elif profile and patient:
            if not patient.birth_date or not patient.blood_type or not patient.emergency_phone:
                needs_medical_profile = True
            if needs_medical_profile:
                notifications.append({
                    "title": "Completa tu ficha médica",
                    "text": "Agrega tus datos de emergencia.",
                    "url": reverse("patient_medical_record", kwargs={"step": 1}),
                    "icon": "notebook-tabs",
                })
            if preferences.get("order_updates", True):
                patient_orders = Order.objects.filter(
                    user_id__in={profile.id, patient.id}
                ).exclude(status__in=["delivered", "cancelled"])[:5]
                for order in patient_orders:
                    notifications.append({
                        "title": "Actualización de pedido",
                        "text": f"{order.order_number} · {order.status}",
                        "url": reverse("patient_order_detail", kwargs={"order_id": order.id}),
                        "icon": "package-check",
                    })
            if preferences.get("qr_activity", True) and patient.last_qr_scan_at:
                notifications.append({
                    "title": "Tu QR fue escaneado",
                    "text": patient.last_qr_scan_at.strftime("%d/%m/%Y %H:%M"),
                    "url": reverse("patient_credential"),
                    "icon": "scan-line",
                })
    except DatabaseError:
        notifications = []
    return {
        "admin_profile": profile if is_admin else None,
        "session_profile": profile,
        "session_patient": patient,
        "is_admin_session": is_admin,
        "admin_email": request.session.get("supabase_email", ""),
        "admin_avatar_url": avatar_url,
        "session_avatar_url": avatar_url,
        "cart_count": cart_count,
        "ticket_count": ticket_count,
        "notifications": notifications[:8],
        "notification_count": len(notifications),
        "needs_medical_profile": needs_medical_profile,
        "demo_mode": settings.DEMO_MODE,
    }
