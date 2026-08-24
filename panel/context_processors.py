from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import F, Q
from django.urls import reverse

from .models import ActivationRequest, DiscountTicket, Invoice, NotificationRead, Order, Patient, Product, Profile
from .services import versioned_media_url
from .patient_utils import medical_profile_is_complete


def _read_link(item):
    query = urlencode({"key": item["key"], "next": item["target"]})
    return {**item, "url": f"{reverse('notification_read')}?{query}"}


def _notification_candidates(profile, patient, is_admin, preferences, needs_medical_profile):
    notifications = []
    if is_admin:
        try:
            activation_requests = ActivationRequest.objects.only(
                "id", "user_id", "email", "created_at"
            ).filter(status="pending").order_by("-created_at")[:5]
            for activation in activation_requests:
                notifications.append({
                    "key": f"admin-activation:{activation.id}",
                    "title": "Solicitud de reactivación",
                    "text": activation.email or "Cuenta bloqueada",
                    "target": reverse("admin_mailbox"), "icon": "user-check",
                })
        except DatabaseError:
            pass
        if preferences.get("system_alerts", True):
            pending_payments = Order.objects.only("id", "order_number", "created_at").exclude(
                payment_proof_path__isnull=True
            ).exclude(payment_proof_path="").filter(payment_reviewed_at__isnull=True).filter(
                Q(payment_rejection_reason__isnull=True) | Q(payment_rejection_reason="")
            ).order_by("-created_at")[:5]
            for order in pending_payments:
                notifications.append({
                    "key": f"admin-payment:{order.id}",
                    "title": "Pago pendiente de verificación", "text": order.order_number,
                    "target": reverse("payment_detail", kwargs={"order_id": order.id}),
                    "icon": "receipt-text",
                })
        if preferences.get("order_updates", True):
            pending_orders = Order.objects.only(
                "id", "order_number", "status", "created_at"
            ).filter(status__in=["pending", "confirmed"]).order_by("-created_at")[:5]
            for order in pending_orders:
                notifications.append({
                    "key": f"admin-order:{order.id}:{order.status}",
                    "title": "Nuevo pedido" if order.status == "pending" else "Pedido confirmado",
                    "text": order.order_number,
                    "target": reverse("order_detail", kwargs={"order_id": order.id}),
                    "icon": "package",
                })
        try:
            low_stock = Product.objects.only("id", "name", "stock", "min_stock").filter(
                is_active=True, stock__lte=F("min_stock")
            ).order_by("stock")[:5]
            for product in low_stock:
                notifications.append({
                    "key": f"admin-low-stock:{product.id}:{product.stock}",
                    "title": "Stock mínimo alcanzado",
                    "text": f"{product.name}: quedan {product.stock}",
                    "target": reverse("products"), "icon": "package-minus",
                })
        except DatabaseError:
            # Permite abrir el panel mientras se ejecuta la actualización SQL.
            pass
    elif patient:
        if needs_medical_profile:
            notifications.append({
                "key": "patient:medical-profile",
                "title": "Completa tu ficha médica", "text": "Agrega tus datos de emergencia.",
                "target": reverse("patient_medical_record", kwargs={"step": 1}),
                "icon": "notebook-tabs",
            })
        if preferences.get("order_updates", True):
            cancelled_orders = Order.objects.only(
                "id", "order_number", "status", "payment_rejection_reason",
                "payment_reviewed_at", "updated_at",
            ).filter(
                user_id__in={profile.id, patient.id}, status="cancelled",
            ).exclude(
                Q(payment_rejection_reason__isnull=True) | Q(payment_rejection_reason="")
            ).order_by("-payment_reviewed_at", "-updated_at")[:5]
            for order in cancelled_orders:
                reviewed_key = order.payment_reviewed_at.isoformat() if order.payment_reviewed_at else "cancelled"
                notifications.append({
                    "key": f"patient-payment-rejected:{order.id}:{reviewed_key}",
                    "title": "Pedido cancelado",
                    "text": f"{order.order_number} · {order.payment_rejection_reason}",
                    "target": reverse("patient_order_detail", kwargs={"order_id": order.id}),
                    "icon": "circle-x",
                })
            patient_orders = Order.objects.only(
                "id", "order_number", "status", "created_at", "estimated_delivery"
            ).filter(user_id__in={profile.id, patient.id}).exclude(
                status__in=["delivered", "cancelled"]
            ).order_by("-created_at")[:5]
            for order in patient_orders:
                if order.status in {"production", "in_production"}:
                    title = "Pago aprobado: pedido en producción"
                    detail = (
                        f"Entrega estimada: {order.estimated_delivery:%d/%m/%Y}"
                        if order.estimated_delivery else order.order_number
                    )
                elif order.status == "shipped":
                    title = "Pedido enviado"
                    detail = "Solicita el código al motorizado para confirmar la entrega."
                else:
                    title = "Actualización de pedido"
                    detail = f"{order.order_number} · {order.status}"
                notifications.append({
                    "key": f"patient-order:{order.id}:{order.status}",
                    "title": title,
                    "text": detail,
                    "target": reverse("patient_order_detail", kwargs={"order_id": order.id}),
                    "icon": "package-check",
                })
        try:
            invoices = Invoice.objects.only("id", "invoice_number", "sent_at").filter(
                user_id__in={profile.id, patient.id}, sent_at__isnull=False
            ).order_by("-sent_at")[:3]
            for invoice in invoices:
                notifications.append({
                    "key": f"patient-invoice:{invoice.id}",
                    "title": "Nueva factura en tu buzón", "text": invoice.invoice_number,
                    "target": reverse("patient_mailbox"), "icon": "file-text",
                })
        except DatabaseError:
            pass
        if preferences.get("qr_activity", True) and patient.last_qr_scan_at:
            notifications.append({
                "key": f"patient-qr:{patient.last_qr_scan_at.isoformat()}",
                "title": "Tu QR fue escaneado",
                "text": patient.last_qr_scan_at.strftime("%d/%m/%Y %H:%M"),
                "target": reverse("patient_credential"), "icon": "scan-line",
            })
    return notifications


def admin_context(request):
    profile = (
        getattr(request, "admin_profile", None)
        or getattr(request, "account_profile", None)
        or getattr(request, "user_profile", None)
        or getattr(request, "_qrmed_session_profile", None)
    )
    if not profile and request.session.get("supabase_user_id"):
        try:
            profile = Profile.objects.only(
                "id", "full_name", "role", "is_active", "avatar_path", "updated_at", "preferences"
            ).get(id=request.session["supabase_user_id"])
        except (Profile.DoesNotExist, ValueError):
            profile = None
    role = (profile.role or "").lower() if profile else ""
    is_admin = role in {"admin", "administrador"}
    patient = getattr(request, "patient", None)
    if profile and not is_admin and patient is None:
        patient = Patient.objects.only(
            "id", "owner_id", "first_name", "last_name", "id_number", "birth_date", "sex",
            "phone", "email", "address", "city", "blood_type", "emergency_name",
            "emergency_relationship", "emergency_phone", "photo_path", "updated_at",
            "last_qr_scan_at",
        ).filter(Q(owner_id=profile.id) | Q(id=profile.id)).order_by("-created_at").first()
    avatar_url = ""
    if profile and profile.avatar_path:
        avatar_url = versioned_media_url(
            reverse("profile_avatar_image", kwargs={"profile_id": profile.id}), profile.updated_at,
        )
    elif patient and patient.photo_path:
        avatar_url = versioned_media_url(
            reverse("patient_photo_image", kwargs={"patient_id": patient.id}), patient.updated_at,
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
    needs_medical_profile = bool(preferences.get("medical_profile_pending"))
    if patient and not medical_profile_is_complete(patient):
        needs_medical_profile = True

    ticket_count = 0
    if profile and not is_admin:
        ticket_cache_key = f"qrmed-ticket-count:{profile.id}"
        ticket_count = cache.get(ticket_cache_key)
        if ticket_count is None:
            try:
                ticket_count = DiscountTicket.objects.filter(user_id=profile.id, used_at__isnull=True).count()
                cache.set(ticket_cache_key, ticket_count, 30)
            except DatabaseError:
                ticket_count = 0

    notifications = []
    if profile:
        cache_key = f"qrmed-notifications:{profile.id}"
        candidates = cache.get(cache_key)
        if candidates is None:
            try:
                candidates = _notification_candidates(profile, patient, is_admin, preferences, needs_medical_profile)
                cache.set(cache_key, candidates, 15)
            except DatabaseError:
                candidates = []
        try:
            read_keys = set(NotificationRead.objects.filter(
                user_id=profile.id,
                notification_key__in=[item["key"] for item in candidates],
            ).values_list("notification_key", flat=True))
        except DatabaseError:
            read_keys = set()
        notifications = [_read_link(item) for item in candidates if item["key"] not in read_keys][:8]

    return {
        "admin_profile": profile if is_admin else None,
        "session_profile": profile, "session_patient": patient,
        "is_admin_session": is_admin,
        "admin_email": request.session.get("supabase_email", ""),
        "admin_avatar_url": avatar_url, "session_avatar_url": avatar_url,
        "cart_count": cart_count, "ticket_count": ticket_count,
        "notifications": notifications, "notification_count": len(notifications),
        "needs_medical_profile": needs_medical_profile,
        "demo_mode": settings.DEMO_MODE,
    }
