import csv
import io
import json
import secrets
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .decorators import admin_required, authenticated_required
from .forms import (
    BankAccountForm, DiscountCampaignForm, OrderUpdateForm, PatientEmergencyForm, PatientMedicalForm, PatientPersonalForm,
    PaymentSettingForm, ProductForm, ProfileForm, RegistrationForm,
)
from .invoice_pdf import build_invoice_pdf
from .models import (
    ActivationRequest, BankAccount, DiscountCampaign, DiscountTicket, Invoice, MedicalDocument,
    NotificationRead, Order, OrderItem, Patient, PaymentSetting, Product, Profile,
)
from .pagination import paginate_items
from .patient_utils import medical_profile_is_complete
from .services import (
    SupabaseError, get_auth_user, request_password_reset, sign_in, sign_up, storage_image_bytes, storage_image_signed_url, storage_signed_url,
    update_password, upload_file, versioned_media_url,
)


def _profile_map(ids):
    return {str(p.id): p for p in Profile.objects.filter(id__in=list(ids))}


def _patient_map(owner_ids):
    """Mapea tanto el owner de Auth como el UUID directo del paciente."""
    result = {}
    identifiers = list(owner_ids)
    for patient in Patient.objects.filter(
        Q(owner_id__in=identifiers) | Q(id__in=identifiers)
    ).order_by("-created_at"):
        result.setdefault(str(patient.owner_id), patient)
        result.setdefault(str(patient.id), patient)
    return result


def _customer_avatar_url(patient=None, profile=None):
    """Usa el avatar de la cuenta; la foto clínica queda como respaldo."""
    if profile:
        return reverse("profile_avatar_image", kwargs={"profile_id": profile.id})
    if patient and patient.photo_path:
        return reverse("patient_photo_image", kwargs={"patient_id": patient.id})
    return ""


def _decorate_bank_assets(bank):
    bank.logo_url = (
        reverse("bank_asset_image", kwargs={"bank_id": bank.id, "kind": "logo"})
        if bank.logo_path else ""
    )
    bank.qr_url = (
        reverse("bank_asset_image", kwargs={"bank_id": bank.id, "kind": "qr"})
        if bank.qr_path else ""
    )
    return bank


def _order_context(order):
    ensure_order_delivery_code(order)
    items = list(OrderItem.objects.filter(order_id=order.id))
    products = {str(p.id): p for p in Product.objects.filter(id__in=[x.product_id for x in items if x.product_id])}
    patient = Patient.objects.filter(
        Q(owner_id=order.user_id) | Q(id=order.user_id)
    ).order_by("-created_at").first()
    profile = Profile.objects.filter(id=order.user_id).first()
    if not profile and patient:
        profile = Profile.objects.filter(id=patient.owner_id).first()
    first_item = items[0] if items else None
    product = products.get(str(first_item.product_id)) if first_item and first_item.product_id else None
    proof_url = storage_signed_url(order.payment_proof_path, settings.SUPABASE_PAYMENT_BUCKET)
    proof_name = Path(urlparse(str(order.payment_proof_path or "")).path).name or "comprobante"
    proof_is_pdf = proof_name.lower().endswith(".pdf")
    try:
        invoice = Invoice.objects.filter(order_id=order.id).first()
    except DatabaseError:
        invoice = None
    customer_phone = str(getattr(patient, "phone", "") or getattr(profile, "phone", "") or order.shipping_phone or "")
    whatsapp_phone = "".join(character for character in customer_phone if character.isdigit())
    if whatsapp_phone.startswith("0"):
        whatsapp_phone = "593" + whatsapp_phone[1:]
    return {
        "order": order,
        "items": items,
        "products_map": products,
        "customer": patient or profile,
        "patient": patient,
        "customer_profile": profile,
        "first_item": first_item,
        "product": product,
        "proof_url": proof_url,
        "proof_name": proof_name,
        "proof_is_pdf": proof_is_pdf,
        "customer_avatar_url": _customer_avatar_url(patient, profile),
        "invoice": invoice,
        "whatsapp_phone": whatsapp_phone,
    }


def _ensure_invoice(order, created_by=None):
    """Crea una sola factura por pedido; el PDF se genera bajo demanda."""
    issued_at = order.payment_reviewed_at or timezone.now()
    invoice, _ = Invoice.objects.get_or_create(
        order_id=order.id,
        defaults={
            "id": uuid.uuid4(), "user_id": order.user_id,
            "invoice_number": f"FAC-{order.order_number}"[:50],
            "issued_at": issued_at, "created_by": created_by,
        },
    )
    return invoice


def generate_delivery_code():
    """Genera un PIN numérico de entrega evitando códigos ya asignados."""
    for _ in range(25):
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not Order.objects.filter(tracking_number=code).exists():
            return code
    return f"{secrets.randbelow(10_000_000):07d}"


def ensure_order_delivery_code(order):
    if not str(order.tracking_number or "").strip():
        order.tracking_number = generate_delivery_code()
        order.updated_at = timezone.now()
        order.save(update_fields=["tracking_number", "updated_at"])
    return order.tracking_number


def _orders_context(request):
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active").strip().lower()
    if status not in {"active", "delivered"}:
        status = "active"
    base_queryset = Order.objects.all()
    if status == "delivered":
        base_queryset = base_queryset.filter(status="delivered")
    else:
        base_queryset = base_queryset.exclude(status="delivered")
    orders_list = list(base_queryset)
    owner_ids = {order.user_id for order in orders_list}
    patients = _patient_map(owner_ids)
    profile_ids = owner_ids | {patient.owner_id for patient in patients.values() if patient.owner_id}
    profiles = _profile_map(profile_ids)
    items = list(OrderItem.objects.filter(order_id__in=[order.id for order in orders_list]))
    item_map = {}
    for item in items:
        item_map.setdefault(str(item.order_id), []).append(item)
    product_map = {
        str(product.id): product
        for product in Product.objects.filter(id__in=[item.product_id for item in items if item.product_id])
    }
    invoice_map = {}
    if status == "delivered":
        try:
            invoice_map = {
                str(invoice.order_id): invoice
                for invoice in Invoice.objects.filter(order_id__in=[order.id for order in orders_list])
            }
        except DatabaseError:
            invoice_map = {}

    rows = []
    for order in orders_list:
        patient = patients.get(str(order.user_id))
        profile = profiles.get(str(order.user_id))
        if not profile and patient:
            profile = profiles.get(str(patient.owner_id))
        order_items = item_map.get(str(order.id), [])
        first_item = order_items[0] if order_items else None
        product = product_map.get(str(first_item.product_id)) if first_item and first_item.product_id else None
        customer_name = patient.full_name if patient else (profile.full_name if profile else "Usuario sin perfil")
        customer_id = patient.id_number if patient else (profile.phone if profile else "")
        normalized_status = str(order.status or "pending").lower()
        haystack = " ".join(filter(None, [order.order_number, customer_name, customer_id, product.name if product else ""]))
        if len(q) >= 2 and q.lower() not in haystack.lower():
            continue
        rows.append({
            "order": order,
            "patient": patient,
            "profile": profile,
            "customer_name": customer_name,
            "customer_id": customer_id,
            "items": order_items,
            "first_item": first_item,
            "product": product,
            "invoice": invoice_map.get(str(order.id)),
        })
    pagination = paginate_items(request, rows)
    return {
        "rows": pagination.pop("items"),
        "orders_total": len(orders_list),
        "q": q,
        "current_status": status,
        **pagination,
    }


def _payment_state(order):
    if order.payment_rejection_reason:
        return "rejected"
    if order.payment_reviewed_at:
        return "approved"
    return "pending"


def _method_kind(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"transfer", "transferencia", "bank_transfer", "transferencia_bancaria"}:
        return "transfer"
    if normalized in {"deposit", "deposito", "depósito", "bank_deposit"}:
        return "deposit"
    return normalized or "other"


def _payments_context(request):
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip().lower()
    method = request.GET.get("method", "").strip().lower()
    orders_list = list(
        Order.objects.exclude(payment_proof_path__isnull=True).exclude(payment_proof_path="")
    )
    owner_ids = {order.user_id for order in orders_list}
    patients = _patient_map(owner_ids)
    profile_ids = owner_ids | {patient.owner_id for patient in patients.values() if patient.owner_id}
    profiles = _profile_map(profile_ids)
    items = list(OrderItem.objects.filter(order_id__in=[order.id for order in orders_list]))
    item_map = {}
    for item in items:
        item_map.setdefault(str(item.order_id), []).append(item)
    products = {
        str(product.id): product
        for product in Product.objects.filter(id__in=[item.product_id for item in items if item.product_id])
    }

    rows = []
    for order in orders_list:
        state = _payment_state(order)
        patient = patients.get(str(order.user_id))
        profile = profiles.get(str(order.user_id))
        if not profile and patient:
            profile = profiles.get(str(patient.owner_id))
        order_items = item_map.get(str(order.id), [])
        first_item = order_items[0] if order_items else None
        product = products.get(str(first_item.product_id)) if first_item and first_item.product_id else None
        customer_name = patient.full_name if patient else (profile.full_name if profile else "Usuario sin perfil")
        customer_id = patient.id_number if patient else "Sin identificación"
        customer_phone = patient.phone if patient else (profile.phone if profile else "")
        customer_email = patient.email if patient else ""
        if status != "all" and state != status:
            continue
        if method and _method_kind(order.payment_method) != method:
            continue
        haystack = " ".join(
            filter(None, [order.order_number, customer_name, customer_id, customer_phone, customer_email])
        ).lower()
        if len(q) >= 2 and q.lower() not in haystack:
            continue
        rows.append({
            "order": order,
            "patient": patient,
            "profile": profile,
            "customer_name": customer_name,
            "customer_id": customer_id,
            "first_item": first_item,
            "product": product,
            "state": state,
            "method_kind": _method_kind(order.payment_method),
            "avatar_url": _customer_avatar_url(patient, profile),
        })

    approved_total = sum(
        (order.total or Decimal("0")) for order in orders_list if _payment_state(order) == "approved"
    )
    pagination = paginate_items(request, rows)
    return {
        "rows": pagination.pop("items"),
        "q": q,
        "current_status": status,
        "current_method": method,
        "pending_count": sum(_payment_state(order) == "pending" for order in orders_list),
        "approved_count": sum(_payment_state(order) == "approved" for order in orders_list),
        "rejected_count": sum(_payment_state(order) == "rejected" for order in orders_list),
        "approved_total": approved_total,
        **pagination,
    }


def _month_labels_and_counts(objects, months=6):
    today = timezone.localdate()
    points = []
    year, month = today.year, today.month
    for offset in reversed(range(months)):
        m = month - offset
        y = year
        while m <= 0:
            m += 12
            y -= 1
        points.append((y, m))
    counter = Counter((obj.created_at.year, obj.created_at.month) for obj in objects if obj.created_at)
    labels_es = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    return [labels_es[m - 1] for y, m in points], [counter[(y, m)] for y, m in points]


def landing(request):
    """Página pública de presentación de QRMed Emergency."""
    return render(request, "panel/landing.html")


def terms(request):
    return render(request, "panel/terms.html")


def privacy(request):
    return render(request, "panel/privacy.html")


def register(request):
    if request.session.get("supabase_user_id"):
        return redirect("patient_dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            result = sign_up(
                data["email"], data["password"], data["first_name"],
                data["last_name"], data["phone"],
            )
            user = result.get("user") or {}
            user_id = uuid.UUID(str(user.get("id")))
            now = timezone.now()
            full_name = f"{data['first_name']} {data['last_name']}".strip()
            with transaction.atomic():
                profile, _ = Profile.objects.update_or_create(
                    id=user_id,
                    defaults={
                        "full_name": full_name, "phone": data["phone"],
                        "role": "usuario", "is_active": True,
                        "preferences": {"medical_profile_pending": True},
                        "updated_at": now,
                    },
                )
                if not profile.created_at:
                    profile.created_at = now
                    profile.save(update_fields=["created_at"])
                if not Patient.objects.filter(Q(owner_id=user_id) | Q(id=user_id)).exists():
                    Patient(
                        id=uuid.uuid4(), owner_id=user_id,
                        first_name=data["first_name"], last_name=data["last_name"],
                        id_number=f"PEND-{uuid.uuid4().hex[:12].upper()}",
                        email=data["email"], phone=data["phone"], qr_token=uuid.uuid4(),
                        status="active", created_at=now, updated_at=now,
                    ).save(force_insert=True)
        except (SupabaseError, DatabaseError, IntegrityError, TypeError, ValueError) as exc:
            messages.error(request, str(exc) or "No se pudo completar el registro.")
        else:
            if result.get("access_token"):
                request.session.cycle_key()
                request.session["supabase_user_id"] = str(user_id)
                request.session["supabase_email"] = data["email"]
                request.session["supabase_access_token"] = result.get("access_token", "")
                request.session["supabase_refresh_token"] = result.get("refresh_token", "")
                request.session["account_role"] = "usuario"
                messages.success(request, "Cuenta creada. Completa ahora tu ficha médica.")
                return redirect("patient_medical_record", step=1)
            messages.success(request, "Cuenta creada. Revisa tu correo para confirmarla e iniciar sesión.")
            return redirect("login")
    return render(request, "panel/register.html", {"form": form})


def login_view(request):
    if request.session.get("supabase_user_id"):
        profile_obj = Profile.objects.filter(id=request.session["supabase_user_id"]).first()
        if profile_obj and (profile_obj.role or "").lower() in {"admin", "administrador"}:
            return redirect("dashboard")
        if profile_obj:
            return redirect("patient_dashboard")
        request.session.flush()
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        try:
            result = sign_in(email, password)
            user = result["user"]
            profile_obj = Profile.objects.filter(id=user["id"]).first()
            if not profile_obj:
                raise SupabaseError("Esta cuenta no tiene un perfil válido.")
            if not profile_obj.is_active:
                if request.POST.get("action") == "request_activation":
                    try:
                        pending_request = ActivationRequest.objects.filter(
                            user_id=profile_obj.id, status="pending",
                        ).first()
                        if pending_request:
                            pending_request.email = user.get("email", email)
                            pending_request.message = "El cliente solicita reactivar su cuenta."
                            pending_request.created_at = timezone.now()
                            pending_request.save(update_fields=["email", "message", "created_at"])
                        else:
                            ActivationRequest(
                                id=uuid.uuid4(), user_id=profile_obj.id,
                                email=user.get("email", email),
                                message="El cliente solicita reactivar su cuenta.",
                                status="pending", created_at=timezone.now(),
                            ).save(force_insert=True)
                        for admin_id in Profile.objects.filter(
                            role__in=["admin", "administrador"], is_active=True,
                        ).values_list("id", flat=True):
                            cache.delete(f"qrmed-notifications:{admin_id}")
                    except DatabaseError:
                        raise SupabaseError("Falta activar el módulo de solicitudes. Ejecuta supabase_actualizacion_completa.sql.")
                    messages.success(request, "Solicitud enviada. El administrador la revisará desde su buzón.")
                    return render(request, "panel/login.html", {"login_email": email})
                messages.error(request, "Tu cuenta está inactiva. Puedes solicitar su reactivación.")
                return render(request, "panel/login.html", {"show_activation_request": True, "login_email": email})
            role = (profile_obj.role or "").lower()
            if role not in {"admin", "administrador", "user", "usuario", "patient", "paciente"}:
                raise SupabaseError("El rol de esta cuenta no está habilitado.")
            request.session.cycle_key()
            request.session["supabase_user_id"] = user["id"]
            request.session["supabase_email"] = user.get("email", email)
            request.session["supabase_access_token"] = result.get("access_token", "")
            request.session["supabase_refresh_token"] = result.get("refresh_token", "")
            request.session["account_role"] = role
            if request.POST.get("remember") != "on":
                request.session.set_expiry(0)
            return redirect("dashboard" if role in {"admin", "administrador"} else "patient_dashboard")
        except SupabaseError as exc:
            messages.error(request, str(exc))
    return render(request, "panel/login.html", {"login_email": request.POST.get("email", "") if request.method == "POST" else ""})


def oauth_start(request, provider):
    if provider != "google":
        return redirect("login")
    if settings.DEMO_MODE or not settings.SUPABASE_URL:
        messages.error(request, "Configura Supabase Auth para habilitar el acceso social.")
        return redirect("login")
    callback = request.build_absolute_uri(reverse("oauth_callback"))
    query = urlencode({"provider": provider, "redirect_to": callback})
    return redirect(f"{settings.SUPABASE_URL}/auth/v1/authorize?{query}")


def password_reset_request(request):
    if request.method == "POST":
        email = str(request.POST.get("email") or "").strip().lower()
        if not email:
            messages.error(request, "Ingresa el correo electrónico de tu cuenta.")
        else:
            try:
                request_password_reset(
                    email,
                    request.build_absolute_uri(reverse("password_reset_page")),
                )
            except SupabaseError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Si el correo está registrado, recibirás un enlace para crear una nueva contraseña.")
                return redirect("login")
    return render(request, "panel/password_reset_request.html")


def password_reset_page(request):
    return render(request, "panel/password_reset.html")


@require_POST
def password_reset_complete(request):
    try:
        payload = json.loads(request.body or "{}")
        access_token = str(payload.get("access_token") or "")
        password = str(payload.get("password") or "")
        confirmation = str(payload.get("confirmation") or "")
        if not access_token:
            raise SupabaseError("El enlace de recuperación es inválido o expiró.")
        if len(password) < 8:
            raise SupabaseError("La nueva contraseña debe tener al menos 8 caracteres.")
        if password != confirmation:
            raise SupabaseError("Las contraseñas no coinciden.")
        update_password(access_token, password)
        return JsonResponse({"ok": True, "redirect": reverse("login")})
    except (json.JSONDecodeError, SupabaseError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


def oauth_callback(request):
    return render(request, "panel/oauth_callback.html")


@require_POST
def oauth_complete(request):
    try:
        payload = json.loads(request.body or "{}")
        access_token = str(payload.get("access_token") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        if not access_token:
            raise SupabaseError("Supabase no devolvió una sesión válida.")
        user = get_auth_user(access_token)
        user_id = uuid.UUID(str(user.get("id")))
        email = str(user.get("email") or "").strip()
        metadata = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
        full_name = str(metadata.get("full_name") or metadata.get("name") or email.split("@")[0] or "Usuario").strip()
        avatar_url = str(metadata.get("avatar_url") or metadata.get("picture") or "").strip()
        now = timezone.now()
        with transaction.atomic():
            profile_obj = Profile.objects.filter(id=user_id).first()
            is_new = profile_obj is None
            if is_new:
                existing_roles = set(Profile.objects.exclude(role__isnull=True).values_list("role", flat=True))
                default_role = "usuario" if existing_roles & {"usuario", "administrador"} else "user"
                profile_obj = Profile(
                    id=user_id, full_name=full_name, role=default_role, is_active=True,
                    avatar_path=avatar_url or None, preferences={"medical_profile_pending": True},
                    created_at=now, updated_at=now,
                )
                profile_obj.save(force_insert=True)
            elif not profile_obj.is_active:
                raise SupabaseError("Esta cuenta está bloqueada.")
            else:
                update_fields = []
                if not profile_obj.full_name and full_name:
                    profile_obj.full_name = full_name
                    update_fields.append("full_name")
                if avatar_url and not profile_obj.avatar_path:
                    profile_obj.avatar_path = avatar_url
                    update_fields.append("avatar_path")
                if update_fields:
                    profile_obj.updated_at = now
                    profile_obj.save(update_fields=[*update_fields, "updated_at"])

            role = str(profile_obj.role or "user").lower()
            if role not in {"admin", "administrador"}:
                patient = Patient.objects.filter(Q(owner_id=user_id) | Q(id=user_id)).first()
                if patient is None:
                    parts = full_name.split(None, 1)
                    Patient(
                        id=uuid.uuid4(), owner_id=user_id, first_name=parts[0] or "Usuario",
                        last_name=parts[1] if len(parts) > 1 else "",
                        id_number=f"PEND-{uuid.uuid4().hex[:12].upper()}", email=email or None,
                        qr_token=uuid.uuid4(), status="active", created_at=now, updated_at=now,
                    ).save(force_insert=True)

        request.session.cycle_key()
        request.session["supabase_user_id"] = str(user_id)
        request.session["supabase_email"] = email
        request.session["supabase_access_token"] = access_token
        request.session["supabase_refresh_token"] = refresh_token
        request.session["account_role"] = role
        destination = reverse("dashboard" if role in {"admin", "administrador"} else "patient_dashboard")
        return JsonResponse({"ok": True, "redirect": destination, "new_account": is_new})
    except (SupabaseError, ValueError, TypeError, DatabaseError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


def logout_view(request):
    request.session.flush()
    return redirect("login")


@authenticated_required
def notification_read(request):
    key = str(request.GET.get("key") or "").strip()[:180]
    destination = str(request.GET.get("next") or "").strip()
    fallback = "dashboard" if (request.account_profile.role or "").lower() in {"admin", "administrador"} else "patient_dashboard"
    if key:
        try:
            NotificationRead.objects.get_or_create(
                user_id=request.account_profile.id,
                notification_key=key,
                defaults={"id": uuid.uuid4(), "read_at": timezone.now()},
            )
            cache.delete(f"qrmed-notifications:{request.account_profile.id}")
        except DatabaseError:
            pass
    if not url_has_allowed_host_and_scheme(destination, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        destination = reverse(fallback)
    return redirect(destination)


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "database_error"}, status=503)


def patient_photo_image(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    access_token = request.session.get("supabase_access_token", "")
    signed_url = storage_image_signed_url(
        settings.SUPABASE_PATIENT_BUCKET,
        patient.photo_path,
        identifiers=(patient.id, patient.owner_id),
        access_token=access_token,
    )
    if signed_url:
        response = HttpResponseRedirect(signed_url)
        response["Cache-Control"] = "private, max-age=300"
        return response
    image = storage_image_bytes(
        settings.SUPABASE_PATIENT_BUCKET,
        patient.photo_path,
        identifiers=(patient.id, patient.owner_id),
        access_token=access_token,
    )
    if not image:
        return HttpResponse(status=404)
    content, content_type = image
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response


@authenticated_required
def bank_asset_image(request, bank_id, kind):
    if kind not in {"logo", "qr"}:
        return HttpResponse(status=404)
    try:
        bank = BankAccount.objects.get(id=bank_id)
    except (BankAccount.DoesNotExist, DatabaseError, ValueError):
        return HttpResponse(status=404)
    role = (getattr(request, "account_profile", None).role or "").lower() if getattr(request, "account_profile", None) else ""
    if not bank.is_visible and role not in {"admin", "administrador"}:
        return HttpResponse(status=404)
    stored_path = bank.logo_path if kind == "logo" else bank.qr_path
    if not stored_path:
        return HttpResponse(status=404)
    access_token = request.session.get("supabase_access_token", "")
    signed_url = storage_image_signed_url(
        settings.SUPABASE_BANK_BUCKET,
        stored_path,
        identifiers=(bank.id,),
        keywords=(kind,),
        access_token=access_token,
    )
    if signed_url:
        response = HttpResponseRedirect(signed_url)
        response["Cache-Control"] = "private, max-age=300"
        return response
    image = storage_image_bytes(
        settings.SUPABASE_BANK_BUCKET,
        stored_path,
        identifiers=(bank.id,),
        keywords=(kind,),
        access_token=access_token,
    )
    if not image:
        return HttpResponse(status=404)
    content, content_type = image
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response


@authenticated_required
def profile_avatar_image(request, profile_id):
    role = (request.account_profile.role or "").lower()
    if role not in {"admin", "administrador"} and request.account_profile.id != profile_id:
        return HttpResponse(status=404)
    profile_obj = get_object_or_404(Profile, id=profile_id)
    access_token = request.session.get("supabase_access_token", "")
    signed_url = storage_image_signed_url(
        settings.SUPABASE_PROFILE_BUCKET,
        profile_obj.avatar_path,
        identifiers=(profile_obj.id,),
        keywords=("avatar",),
        access_token=access_token,
    )
    if signed_url:
        response = HttpResponseRedirect(signed_url)
        response["Cache-Control"] = "private, max-age=300"
        return response
    image = storage_image_bytes(
        settings.SUPABASE_PROFILE_BUCKET,
        profile_obj.avatar_path,
        identifiers=(profile_obj.id,),
        keywords=("avatar",),
        access_token=access_token,
    )
    if not image:
        return HttpResponse(status=404)
    content, content_type = image
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response


@authenticated_required
def profile_cover_image(request, profile_id):
    role = (request.account_profile.role or "").lower()
    if role not in {"admin", "administrador"} and request.account_profile.id != profile_id:
        return HttpResponse(status=404)
    profile_obj = get_object_or_404(Profile, id=profile_id)
    access_token = request.session.get("supabase_access_token", "")
    signed_url = storage_image_signed_url(
        settings.SUPABASE_PROFILE_BUCKET,
        profile_obj.cover_path,
        identifiers=(profile_obj.id,),
        keywords=("cover", "portada"),
        access_token=access_token,
    )
    if signed_url:
        response = HttpResponseRedirect(signed_url)
        response["Cache-Control"] = "private, max-age=300"
        return response
    image = storage_image_bytes(
        settings.SUPABASE_PROFILE_BUCKET,
        profile_obj.cover_path,
        identifiers=(profile_obj.id,),
        keywords=("cover", "portada"),
        access_token=access_token,
    )
    if not image:
        return HttpResponse(status=404)
    content, content_type = image
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response


@admin_required
def dashboard(request):
    patients_qs = Patient.objects.all()
    orders_qs = Order.objects.all()
    patients_list = list(patients_qs)
    orders_list = list(orders_qs)
    active = sum(1 for p in patients_list if str(p.status).lower() == "active")
    labels, patient_counts = _month_labels_and_counts(patients_list)
    _, order_counts = _month_labels_and_counts(orders_list)
    recent_orders = orders_list[:4]
    recent_patients = patients_list[:5]
    context = {
        "patient_count": len(patients_list), "qr_count": len(patients_list), "order_count": len(orders_list),
        "activation_rate": round(active / len(patients_list) * 100) if patients_list else 0,
        "active_count": active, "recent_patients": recent_patients, "recent_orders": recent_orders,
        "chart_labels": json.dumps(labels), "patient_chart": json.dumps(patient_counts), "order_chart": json.dumps(order_counts),
    }
    return render(request, "panel/dashboard.html", context)


@admin_required
def global_search(request):
    q = request.GET.get("q", "").strip()
    modules = [
        ("Inicio", "Resumen del panel", "dashboard"),
        ("Pacientes", "Fichas médicas y códigos QR", "patients"),
        ("Validar pagos", "Comprobantes pendientes", "payments"),
        ("Productos", "Catálogo de pulseras", "products"),
        ("Pedidos", "Seguimiento y entregas", "orders"),
        ("Usuarios", "Accesos y permisos", "users"),
        ("Perfil", "Datos de la cuenta", "profile"),
        ("Bancos y cuentas", "Datos para recibir pagos", "banks"),
        ("Tickets de descuento", "Campañas, códigos y límites", "discounts"),
        ("Configuración", "Preferencias y notificaciones", "configuration"),
    ]
    results = [
        {"title": title, "subtitle": subtitle, "url": reverse(url_name)}
        for title, subtitle, url_name in modules
        if not q or q.casefold() in f"{title} {subtitle}".casefold()
    ]
    if len(q) >= 2:
        for p in Patient.objects.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(id_number__icontains=q))[:6]:
            results.append({"title": p.full_name, "subtitle": f"Paciente · {p.id_number}", "url": f"/pacientes/{p.id}/"})
        for o in Order.objects.filter(order_number__icontains=q)[:5]:
            results.append({"title": o.order_number, "subtitle": "Pedido", "url": f"/pedidos/{o.id}/"})
        for p in Product.objects.filter(name__icontains=q)[:5]:
            results.append({"title": p.name, "subtitle": "Producto", "url": f"/productos/?q={p.name}"})
    return JsonResponse({"results": results})


@admin_required
def patients(request):
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip().lower()
    qs = Patient.objects.all()
    if len(q) >= 2:
        qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(id_number__icontains=q) | Q(email__icontains=q) | Q(city__icontains=q))
    if status in {"active", "inactive"}:
        qs = qs.filter(status=status)
    pagination = paginate_items(request, qs)
    patient_rows = pagination.pop("items")
    profiles = _profile_map({patient.owner_id for patient in patient_rows if patient.owner_id})
    for patient in patient_rows:
        profile = profiles.get(str(patient.owner_id))
        patient.account_avatar_url = (
            reverse("profile_avatar_image", kwargs={"profile_id": profile.id})
            if profile else ""
        )
        patient.qr_enabled = medical_profile_is_complete(patient)
    return render(request, "panel/patients.html", {
        "patients": patient_rows,
        "q": q,
        "current_status": status,
        "total_patients": Patient.objects.count(),
        **pagination,
    })


@admin_required
def patient_create(request):
    patient = Patient(id=uuid.uuid4(), owner_id=request.admin_profile.id, qr_token=uuid.uuid4(), status="active")
    form = PatientPersonalForm(request.POST or None, request.FILES or None, instance=patient)
    if request.method == "POST" and form.is_valid():
        patient = form.save(commit=False)
        patient.owner_id = request.admin_profile.id
        patient.created_at = timezone.now()
        patient.updated_at = timezone.now()
        if request.FILES.get("photo"):
            try:
                patient.photo_path = upload_file(
                    request.FILES["photo"], settings.SUPABASE_PATIENT_BUCKET,
                    f"{patient.owner_id}/{patient.id}", "photo-",
                    request.session.get("supabase_access_token", ""),
                )
            except SupabaseError as exc:
                messages.error(request, str(exc))
                return render(request, "panel/patient_edit.html", {"patient": patient, "form": form, "step": 1, "is_create": True})
        try:
            patient.save(force_insert=True)
        except IntegrityError as exc:
            if "patients_sex_check" not in str(exc):
                raise
            form.add_error(
                "sex" if "sex" in form.fields else None,
                "Selecciona nuevamente el sexo del paciente.",
            )
            return render(request, "panel/patient_edit.html", {
                "patient": patient, "form": form, "step": 1, "is_create": True,
            })
        messages.success(request, "Datos personales guardados. Completa la información médica.")
        return redirect("patient_edit", patient_id=patient.id, step=2)
    return render(request, "panel/patient_edit.html", {"patient": patient, "form": form, "step": 1, "is_create": True})


@admin_required
def patients_export(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="pacientes_qrmed.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Paciente", "Identificación", "Correo", "Teléfono", "Sangre", "Ciudad", "Estado"])
    for patient in Patient.objects.all():
        writer.writerow([patient.full_name, patient.id_number, patient.email, patient.phone, patient.blood_type, patient.city, patient.status])
    return response


@admin_required
def patient_detail(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    return render(request, "panel/patient_detail.html", {
        "patient": patient,
        "qr_enabled": medical_profile_is_complete(patient),
    })


@admin_required
def patient_qr_image(request, patient_id):
    import qrcode

    patient = get_object_or_404(Patient, id=patient_id)
    if not medical_profile_is_complete(patient):
        return HttpResponse("Completa la ficha médica antes de generar el QR.", status=409)
    public_url = request.build_absolute_uri(reverse("public_patient", kwargs={"token": patient.qr_token}))
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(public_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#183b62", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    response = HttpResponse(output.getvalue(), content_type="image/png")
    response["Cache-Control"] = "private, max-age=300"
    return response


@admin_required
@require_POST
def patient_delete(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    MedicalDocument.objects.filter(patient_id=patient.id).delete()
    patient.delete()
    messages.success(request, "Paciente eliminado correctamente.")
    return redirect("patients")


def public_patient(request, token):
    patient = get_object_or_404(Patient, qr_token=token)
    profile_obj = Profile.objects.filter(id=patient.owner_id).first()
    if patient.status != "active" or (profile_obj and not profile_obj.is_active):
        return render(request, "panel/public_patient_inactive.html", status=403)
    preferences = profile_obj.preferences if profile_obj and isinstance(profile_obj.preferences, dict) else {}
    if preferences.get("public_profile", True) is False:
        return HttpResponse("Esta ficha de emergencia está configurada como privada.", status=403)
    if not medical_profile_is_complete(patient):
        return HttpResponse("Esta ficha todavía no está completa y su código QR no ha sido habilitado.", status=409)
    if preferences.get("analytics", True):
        Patient.objects.filter(id=patient.id).update(
            qr_scan_count=F("qr_scan_count") + 1,
            last_qr_scan_at=timezone.now(),
        )
    phone_digits = "".join(character for character in str(patient.emergency_phone or "") if character.isdigit())
    if phone_digits.startswith("0"):
        phone_digits = "593" + phone_digits[1:]
    whatsapp_text = quote(
        f"Hola, escaneé la manilla QRMed de {patient.full_name}. "
        "Me comunico por una situación relacionada con su ficha de emergencia."
    )
    return render(request, "panel/public_patient.html", {
        "patient": patient,
        "whatsapp_url": f"https://wa.me/{phone_digits}?text={whatsapp_text}" if phone_digits else "",
    })


@admin_required
def patient_edit(request, patient_id, step):
    patient = get_object_or_404(Patient, id=patient_id)
    form_classes = {1: PatientPersonalForm, 2: PatientMedicalForm, 3: PatientEmergencyForm}
    if step not in form_classes:
        return redirect("patient_edit", patient_id=patient.id, step=1)
    form = form_classes[step](request.POST or None, request.FILES or None, instance=patient)
    if request.method == "POST" and form.is_valid():
        patient = form.save(commit=False)
        if step == 1 and request.FILES.get("photo"):
            try:
                patient.photo_path = upload_file(
                    request.FILES["photo"], settings.SUPABASE_PATIENT_BUCKET,
                    f"{patient.owner_id}/{patient.id}", "photo-",
                    request.session.get("supabase_access_token", ""),
                )
            except SupabaseError as exc:
                messages.error(request, str(exc))
                return render(request, "panel/patient_edit.html", {"patient": patient, "form": form, "step": step, "is_create": False})
        patient.updated_at = timezone.now()
        try:
            patient.save()
        except IntegrityError as exc:
            if "patients_sex_check" not in str(exc):
                raise
            form.add_error(
                "sex" if "sex" in form.fields else None,
                "Selecciona nuevamente el sexo del paciente.",
            )
            return render(request, "panel/patient_edit.html", {
                "patient": patient, "form": form, "step": step, "is_create": False,
            })
        if step < 3:
            return redirect("patient_edit", patient_id=patient.id, step=step + 1)
        messages.success(request, "Paciente actualizado correctamente.")
        return redirect("patient_detail", patient_id=patient.id)
    return render(request, "panel/patient_edit.html", {"patient": patient, "form": form, "step": step, "is_create": False})


@admin_required
def payments(request):
    return render(request, "panel/payments.html", _payments_context(request))


@admin_required
def payment_detail(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    context = _payments_context(request)
    context.update(_order_context(order))
    context["payment_state"] = _payment_state(order)
    return render(request, "panel/payment_detail.html", context)


@admin_required
@require_POST
def payment_review(request, order_id, action):
    order = get_object_or_404(Order, id=order_id)
    if action == "approve":
        order.payment_rejection_reason = None
        order.payment_reviewed_at = timezone.now()
        order.payment_reviewed_by = request.admin_profile.id
        if order.status in {"pending", "confirmed"}:
            existing_statuses = set(Order.objects.values_list("status", flat=True))
            order.status = "in_production" if "in_production" in existing_statuses else "production"
        messages.success(request, "Pago aprobado correctamente.")
    elif action == "reject":
        reason = request.POST.get("reason", "Comprobante no válido").strip()
        order.payment_rejection_reason = reason
        order.payment_reviewed_at = timezone.now()
        order.payment_reviewed_by = request.admin_profile.id
        messages.success(request, "Pago rechazado y marcado para revisión del cliente.")
    else:
        messages.error(request, "Acción de pago no válida.")
        return redirect("payments")
    order.updated_at = timezone.now()
    order.save()
    if action == "approve":
        try:
            _ensure_invoice(order, request.admin_profile.id)
        except DatabaseError:
            messages.warning(request, "El pago se aprobó, pero debes ejecutar supabase_actualizacion_completa.sql para generar la factura.")
        cache.delete(f"qrmed-notifications:{order.user_id}")
        return redirect("payment_detail", order_id=order.id)
    return redirect("payments")


@admin_required
@require_POST
def invoice_send(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not order.payment_reviewed_at or order.payment_rejection_reason:
        messages.error(request, "Primero debes aprobar el pago.")
        return redirect("payment_detail", order_id=order.id)
    try:
        invoice = _ensure_invoice(order, request.admin_profile.id)
        if not invoice.sent_at:
            invoice.sent_at = timezone.now()
            invoice.save(update_fields=["sent_at"])
        cache.delete(f"qrmed-notifications:{order.user_id}")
        messages.success(request, "Factura enviada al buzón del cliente.")
    except DatabaseError:
        messages.error(request, "No se pudo enviar la factura. Ejecuta supabase_actualizacion_completa.sql en Supabase.")
    return redirect("payment_detail", order_id=order.id)


@authenticated_required
def invoice_pdf(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id)
    profile = request.account_profile
    role = str(profile.role or "").lower()
    if role not in {"admin", "administrador"}:
        patient_ids = set(Patient.objects.filter(owner_id=profile.id).values_list("id", flat=True))
        if invoice.user_id not in {profile.id, *patient_ids}:
            return HttpResponse("No autorizado", status=403)
    order = get_object_or_404(Order, id=invoice.order_id)
    items = list(OrderItem.objects.filter(order_id=order.id))
    products = {
        str(product.id): product
        for product in Product.objects.filter(id__in=[item.product_id for item in items if item.product_id])
    }
    patient = Patient.objects.filter(Q(owner_id=order.user_id) | Q(id=order.user_id)).order_by("-created_at").first()
    customer = patient or Profile.objects.filter(id=order.user_id).first()
    payment = PaymentSetting.objects.first()
    company = {
        "name": settings.QRMED_COMPANY_NAME,
        "tax_id": getattr(payment, "tax_id", "") or settings.QRMED_COMPANY_TAX_ID,
        "address": settings.QRMED_COMPANY_ADDRESS,
        "phone": settings.QRMED_COMPANY_PHONE,
        "email": getattr(payment, "notification_email", "") or settings.QRMED_COMPANY_EMAIL,
    }
    payload = build_invoice_pdf(
        invoice=invoice, order=order, items=items, products=products,
        customer=customer, company=company,
    )
    response = HttpResponse(payload, content_type="application/pdf")
    disposition = "inline" if request.GET.get("preview") == "1" else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="factura-{invoice.invoice_number}.pdf"'
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@admin_required
def payments_export(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="pagos_qrmed.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Pedido", "Método", "Total", "Estado", "Fecha"])
    for order in Order.objects.exclude(payment_proof_path__isnull=True):
        writer.writerow([order.order_number, order.payment_method, order.total, _payment_state(order), order.created_at])
    return response


@admin_required
def products(request):
    editing = Product.objects.filter(id=request.GET.get("edit")).first() if request.GET.get("edit") else None
    form = ProductForm(request.POST or None, instance=editing)
    if request.method == "POST" and form.is_valid():
        product = form.save(commit=False)
        now = timezone.now()
        product.updated_at = now
        if not product.created_at:
            product.created_at = now
        product.save()
        messages.success(request, "Producto guardado correctamente.")
        return redirect("products")
    q = request.GET.get("q", "").strip()
    qs = Product.objects.all()
    if len(q) >= 2:
        qs = qs.filter(name__icontains=q)
    products_count = qs.count()
    pagination = paginate_items(request, qs)
    return render(request, "panel/products.html", {
        "products": pagination.pop("items"),
        "products_count": products_count,
        "form": form,
        "editing": editing,
        "q": q,
        **pagination,
    })


@admin_required
@require_POST
def product_edit(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    form = ProductForm(request.POST, instance=product)
    if form.is_valid():
        item = form.save(commit=False)
        item.updated_at = timezone.now()
        item.save()
        messages.success(request, "Producto actualizado.")
    else:
        messages.error(request, "Revisa los datos del producto.")
    return redirect("products")


@admin_required
@require_POST
def product_delete(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    product.delete()
    messages.success(request, "Producto eliminado.")
    return redirect("products")


def _new_discount_code():
    for _ in range(30):
        code = f"QRMED-{secrets.token_hex(3).upper()}"
        if not DiscountCampaign.objects.filter(code__iexact=code).exists():
            return code
    return f"QRMED-{uuid.uuid4().hex[:10].upper()}"


@admin_required
def discounts(request):
    schema_ready = True
    campaigns = []
    editing = None
    requested_id = request.POST.get("campaign_id") if request.method == "POST" else request.GET.get("edit")
    try:
        if requested_id:
            editing = DiscountCampaign.objects.filter(id=requested_id).first()
        campaigns = list(DiscountCampaign.objects.all())
    except (DatabaseError, ValueError):
        schema_ready = False

    instance = editing or DiscountCampaign(id=uuid.uuid4(), is_active=True, max_claims=10)
    form = DiscountCampaignForm(request.POST or None, instance=instance)
    if request.method == "POST":
        if not schema_ready:
            messages.error(request, "Primero ejecuta supabase_descuentos.sql en Supabase.")
        elif form.is_valid():
            campaign = form.save(commit=False)
            is_new = editing is None
            if is_new:
                campaign.id = instance.id or uuid.uuid4()
                campaign.created_by = request.admin_profile.id
                campaign.created_at = timezone.now()
            campaign.code = campaign.code or _new_discount_code()
            campaign.updated_at = timezone.now()
            try:
                campaign.save(force_insert=is_new)
            except DatabaseError:
                messages.error(request, "No se pudo guardar. Verifica que el código no esté repetido.")
            else:
                messages.success(request, "Campaña de tickets creada." if is_new else "Campaña actualizada.")
                return redirect("discounts")
        else:
            messages.error(request, "Revisa los datos del ticket.")

    now = timezone.now()
    if schema_ready:
        for campaign in campaigns:
            campaign.claimed_count = DiscountTicket.objects.filter(campaign_id=campaign.id).count()
            campaign.used_count = DiscountTicket.objects.filter(campaign_id=campaign.id, used_at__isnull=False).count()
            campaign.remaining_count = max(0, campaign.max_claims - campaign.claimed_count)
            campaign.is_available_now = bool(
                campaign.is_active
                and (not campaign.starts_at or campaign.starts_at <= now)
                and (not campaign.expires_at or campaign.expires_at > now)
                and campaign.remaining_count > 0
            )
    pagination = paginate_items(request, campaigns)
    return render(request, "panel/discounts.html", {
        "form": form,
        "campaigns": pagination.pop("items"),
        "editing": editing,
        "schema_ready": schema_ready,
        "campaigns_total": len(campaigns),
        "tickets_total": sum(getattr(item, "claimed_count", 0) for item in campaigns),
        "tickets_available": sum(getattr(item, "remaining_count", 0) for item in campaigns if item.is_active),
        **pagination,
    })


@admin_required
@require_POST
def discount_toggle(request, campaign_id):
    campaign = get_object_or_404(DiscountCampaign, id=campaign_id)
    campaign.is_active = not campaign.is_active
    campaign.updated_at = timezone.now()
    campaign.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Campaña activada." if campaign.is_active else "Campaña pausada.")
    return redirect("discounts")


@admin_required
def orders(request):
    return render(request, "panel/orders.html", _orders_context(request))


@admin_required
def order_detail(request, order_id):
    context = _order_context(get_object_or_404(Order, id=order_id))
    context["form"] = OrderUpdateForm(instance=context["order"])
    context.update({f"list_{key}": value for key, value in _orders_context(request).items()})
    return render(request, "panel/order_detail.html", context)


@admin_required
@require_POST
def order_update(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if str(order.status or "").lower() == "delivered":
        messages.info(request, "Un pedido entregado queda cerrado y ya no puede modificarse.")
        return redirect("order_detail", order_id=order.id)
    original_delivery_code = ensure_order_delivery_code(order)
    form = OrderUpdateForm(request.POST, instance=order)
    if form.is_valid():
        item = form.save(commit=False)
        item.tracking_number = original_delivery_code
        item.updated_at = timezone.now()
        item.save()
        cache.delete(f"qrmed-notifications:{order.user_id}")
        messages.success(request, "Pedido actualizado correctamente.")
    else:
        messages.error(request, "No se pudo actualizar el pedido.")
    return redirect("order_detail", order_id=order.id)


@admin_required
@require_POST
def order_delete(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if str(order.status or "").lower() == "delivered":
        messages.error(request, "Los pedidos entregados forman parte del historial y no se pueden eliminar.")
        return redirect("order_detail", order_id=order.id)
    OrderItem.objects.filter(order_id=order.id).delete()
    order.delete()
    messages.success(request, "Pedido eliminado.")
    return redirect("orders")


@admin_required
def users(request):
    q = request.GET.get("q", "").strip()
    all_profiles = Profile.objects.all().order_by("-created_at")
    qs = all_profiles
    if len(q) >= 2:
        qs = qs.filter(Q(full_name__icontains=q) | Q(phone__icontains=q))
    roles = [str(role or "").lower() for role in all_profiles.values_list("role", flat=True)]
    pagination = paginate_items(request, qs)
    return render(request, "panel/users.html", {
        "profiles": pagination.pop("items"),
        "q": q,
        "users_total": all_profiles.count(),
        "admins_total": sum(role in {"admin", "administrador"} for role in roles),
        "active_total": all_profiles.filter(is_active=True).count(),
        "blocked_total": all_profiles.filter(is_active=False).count(),
        **pagination,
    })


@admin_required
@require_POST
def user_role(request, user_id):
    target = get_object_or_404(Profile, id=user_id)
    role = request.POST.get("role", "user").lower()
    if role not in {"user", "admin"}:
        messages.error(request, "Rol no válido.")
    elif target.id == request.admin_profile.id and role == "user":
        messages.error(request, "No puedes quitarte tu propio rol de administrador.")
    else:
        # Algunos proyectos antiguos usan valores en español y otros usan
        # los enum habituales de Supabase: user/admin. Conservamos el estilo
        # que ya existe en la base para no romper el tipo user_role.
        current_roles = set(Profile.objects.exclude(role__isnull=True).values_list("role", flat=True))
        uses_spanish_enum = bool(current_roles & {"usuario", "administrador"})
        target.role = ("administrador" if role == "admin" else "usuario") if uses_spanish_enum else role
        target.updated_at = timezone.now()
        target.save()
        messages.success(request, "Rol actualizado.")
    return redirect("users")


@admin_required
@require_POST
def user_status(request, user_id):
    target = get_object_or_404(Profile, id=user_id)
    if target.id == request.admin_profile.id:
        messages.error(request, "No puedes bloquear tu propia cuenta.")
    else:
        target.is_active = not target.is_active
        target.updated_at = timezone.now()
        target.save()
        Patient.objects.filter(Q(owner_id=target.id) | Q(id=target.id)).update(
            status="active" if target.is_active else "inactive",
            updated_at=timezone.now(),
        )
        cache.delete(f"qrmed-notifications:{target.id}")
        messages.success(request, "Estado de usuario actualizado.")
    return redirect("users")


@admin_required
def admin_mailbox(request):
    status = request.GET.get("status", "pending").strip().lower()
    if status not in {"pending", "approved", "rejected", "all"}:
        status = "pending"
    try:
        queryset = ActivationRequest.objects.all()
        if status != "all":
            queryset = queryset.filter(status=status)
        requests_list = list(queryset)
        profile_map = _profile_map({item.user_id for item in requests_list})
        rows = [{"request": item, "profile": profile_map.get(str(item.user_id))} for item in requests_list]
        schema_ready = True
    except DatabaseError:
        rows, schema_ready = [], False
    pagination = paginate_items(request, rows)
    return render(request, "panel/admin_mailbox.html", {
        "activation_rows": pagination.pop("items"), "current_status": status,
        "schema_ready": schema_ready, **pagination,
    })


@admin_required
@require_POST
def activation_review(request, request_id, action):
    if action not in {"approve", "reject"}:
        messages.error(request, "Acción no válida.")
        return redirect("admin_mailbox")
    activation = get_object_or_404(ActivationRequest, id=request_id)
    if activation.status != "pending":
        messages.info(request, "Esta solicitud ya fue revisada.")
        return redirect("admin_mailbox")
    activation.status = "approved" if action == "approve" else "rejected"
    activation.reviewed_at = timezone.now()
    activation.reviewed_by = request.admin_profile.id
    activation.save(update_fields=["status", "reviewed_at", "reviewed_by"])
    if action == "approve":
        Profile.objects.filter(id=activation.user_id).update(is_active=True, updated_at=timezone.now())
        Patient.objects.filter(Q(owner_id=activation.user_id) | Q(id=activation.user_id)).update(
            status="active", updated_at=timezone.now(),
        )
        messages.success(request, "Cuenta y código QR reactivados.")
    else:
        messages.success(request, "Solicitud rechazada.")
    cache.delete(f"qrmed-notifications:{request.admin_profile.id}")
    return redirect("admin_mailbox")


@admin_required
def profile(request):
    patient_count = Patient.objects.count()
    profile_obj = request.admin_profile
    return render(request, "panel/profile.html", {
        "profile_form": ProfileForm(instance=profile_obj),
        "patient_count": patient_count, "order_count": Order.objects.count(),
        "profile_cover_url": versioned_media_url(
            reverse("profile_cover_image", kwargs={"profile_id": profile_obj.id}),
            profile_obj.updated_at,
        ) if profile_obj.cover_path else "",
        "profile_avatar_url": versioned_media_url(
            reverse("profile_avatar_image", kwargs={"profile_id": profile_obj.id}),
            profile_obj.updated_at,
        ) if profile_obj.avatar_path else "",
        "profile_display_name": profile_obj.full_name or "Administrador",
        "profile_email": request.session.get("supabase_email", ""),
        "profile_phone": profile_obj.phone or "",
        "profile_city": profile_obj.city or "",
        "profile_specialty": profile_obj.specialty or "Administración",
        "profile_specialty_label": "Especialidad",
        "profile_role_label": "Administrador",
        "profile_role_icon": "shield-check",
        "profile_edit_action": reverse("profile_edit"),
        "profile_password_action": reverse("password_update"),
        "profile_metric_primary_label": "Pacientes registrados",
        "profile_metric_primary": patient_count,
        "profile_metric_qr_label": "QR generados",
        "profile_metric_qr": patient_count,
        "profile_metric_orders": Order.objects.count(),
        "profile_member_since": profile_obj.created_at,
        "profile_danger_text": "La eliminación de una cuenta es irreversible y requiere gestión directa en Supabase Auth.",
    })


@admin_required
@require_POST
def profile_edit(request):
    form = ProfileForm(request.POST, request.FILES, instance=request.admin_profile)
    if form.is_valid():
        profile_obj = form.save(commit=False)
        try:
            if request.FILES.get("avatar"):
                profile_obj.avatar_path = upload_file(
                    request.FILES["avatar"], settings.SUPABASE_PROFILE_BUCKET,
                    str(profile_obj.id), "avatar-",
                    request.session.get("supabase_access_token", ""),
                )
            if request.FILES.get("cover"):
                profile_obj.cover_path = upload_file(
                    request.FILES["cover"], settings.SUPABASE_PROFILE_BUCKET,
                    str(profile_obj.id), "cover-",
                    request.session.get("supabase_access_token", ""),
                )
        except SupabaseError as exc:
            messages.error(request, str(exc))
            return redirect("profile")
        profile_obj.updated_at = timezone.now()
        update_fields = list(form._meta.fields) + ["updated_at"]
        if request.FILES.get("avatar"):
            update_fields.append("avatar_path")
        if request.FILES.get("cover"):
            update_fields.append("cover_path")
        profile_obj.save(update_fields=list(dict.fromkeys(update_fields)))
        messages.success(request, "Perfil actualizado correctamente.")
    else:
        messages.error(request, "Revisa la información del perfil.")
    return redirect("profile")


@admin_required
@require_POST
def password_update(request):
    current_password = request.POST.get("current_password", "")
    new_password = request.POST.get("new_password", "")
    confirm = request.POST.get("confirm_password", "")
    if not current_password:
        messages.error(request, "Ingresa tu contraseña actual.")
    elif len(new_password) < 8:
        messages.error(request, "La nueva contraseña debe tener al menos 8 caracteres.")
    elif new_password != confirm:
        messages.error(request, "Las contraseñas no coinciden.")
    else:
        try:
            auth_result = sign_in(request.session.get("supabase_email", ""), current_password)
            update_password(auth_result.get("access_token", ""), new_password)
            request.session["supabase_access_token"] = auth_result.get("access_token", "")
            request.session["supabase_refresh_token"] = auth_result.get("refresh_token", "")
            messages.success(request, "Contraseña actualizada.")
        except SupabaseError as exc:
            messages.error(request, str(exc))
    return redirect("profile")


@admin_required
def banks(request):
    """Administra múltiples cuentas bancarias publicadas para el checkout."""
    schema_ready = True
    editing_bank = None
    bank_rows = []
    requested_id = request.POST.get("bank_id") if request.method == "POST" else request.GET.get("edit")

    try:
        if requested_id:
            editing_bank = BankAccount.objects.filter(id=requested_id).first()
        bank_rows = list(BankAccount.objects.all().order_by("display_order", "bank_name"))
    except (DatabaseError, ValueError):
        schema_ready = False
        editing_bank = None
        bank_rows = []

    instance = editing_bank or BankAccount(id=uuid.uuid4(), is_visible=True, display_order=0)
    form = BankAccountForm(request.POST or None, request.FILES or None, instance=instance)

    if request.method == "POST":
        if not schema_ready:
            messages.error(request, "Primero ejecuta el archivo supabase_bancos.sql en Supabase.")
        elif form.is_valid():
            bank = form.save(commit=False)
            is_new = editing_bank is None
            if is_new:
                bank.id = instance.id or uuid.uuid4()
                bank.created_by = request.admin_profile.id
                bank.created_at = timezone.now()
            bank.updated_at = timezone.now()
            bank.logo_path = editing_bank.logo_path if editing_bank else ""
            bank.qr_path = editing_bank.qr_path if editing_bank else ""
            access_token = request.session.get("supabase_access_token", "")
            try:
                if request.FILES.get("logo"):
                    bank.logo_path = upload_file(
                        request.FILES["logo"],
                        settings.SUPABASE_BANK_BUCKET,
                        f"banks/{bank.id}",
                        "logo-",
                        access_token,
                    )
                if request.FILES.get("qr_image"):
                    bank.qr_path = upload_file(
                        request.FILES["qr_image"],
                        settings.SUPABASE_BANK_BUCKET,
                        f"banks/{bank.id}",
                        "qr-",
                        access_token,
                    )
            except SupabaseError as exc:
                messages.error(request, str(exc))
            else:
                try:
                    bank.save(force_insert=is_new)
                except DatabaseError:
                    messages.error(request, "No se pudo guardar el banco. Verifica que supabase_bancos.sql se haya ejecutado completo.")
                else:
                    messages.success(request, "Banco guardado correctamente." if is_new else "Datos bancarios actualizados.")
                    return redirect("banks")
        else:
            messages.error(request, "Revisa los campos marcados antes de guardar.")

    for bank in bank_rows:
        _decorate_bank_assets(bank)
    if editing_bank:
        _decorate_bank_assets(editing_bank)

    return render(request, "panel/banks.html", {
        "form": form,
        "banks": bank_rows,
        "editing_bank": editing_bank,
        "schema_ready": schema_ready,
        "visible_count": sum(bool(bank.is_visible) for bank in bank_rows),
    })


@admin_required
@require_POST
def bank_toggle_visibility(request, bank_id):
    try:
        bank = BankAccount.objects.get(id=bank_id)
        bank.is_visible = not bank.is_visible
        bank.updated_at = timezone.now()
        bank.save(update_fields=["is_visible", "updated_at"])
    except BankAccount.DoesNotExist:
        messages.error(request, "La cuenta bancaria ya no existe.")
    except DatabaseError:
        messages.error(request, "No se pudo actualizar la cuenta. Verifica el esquema de Supabase.")
    else:
        messages.success(request, "Cuenta publicada para pacientes." if bank.is_visible else "Cuenta ocultada para pacientes.")
    return redirect("banks")


@admin_required
def configuration(request):
    payment_setting = PaymentSetting.objects.first() or PaymentSetting(id=True)
    payment_form = PaymentSettingForm(instance=payment_setting)
    preferences = request.admin_profile.preferences or {}
    if request.method == "POST":
        section = request.POST.get("section")
        if section == "preferences":
            boolean_keys = ["order_updates", "qr_activity", "email_news", "system_alerts", "public_profile", "analytics"]
            preferences = {**preferences, "language": request.POST.get("language", "es"), "theme": request.POST.get("theme", "light")}
            preferences.update({key: request.POST.get(key) == "on" for key in boolean_keys})
            request.admin_profile.preferences = preferences
            request.admin_profile.updated_at = timezone.now()
            request.admin_profile.save()
            messages.success(request, "Configuración guardada.")
        elif section == "payments":
            payment_form = PaymentSettingForm(request.POST, instance=payment_setting)
            if payment_form.is_valid():
                obj = payment_form.save(commit=False)
                obj.id = True
                obj.updated_at = timezone.now()
                obj.save()
                messages.success(request, "Datos bancarios actualizados.")
            else:
                messages.error(request, "Revisa los datos bancarios.")
                return render(request, "panel/configuration.html", {
                    "preferences": preferences,
                    "payment_form": payment_form,
                    "profile_bucket": settings.SUPABASE_PROFILE_BUCKET,
                    "patient_bucket": settings.SUPABASE_PATIENT_BUCKET,
                    "payment_bucket": settings.SUPABASE_PAYMENT_BUCKET,
                    "bank_bucket": settings.SUPABASE_BANK_BUCKET,
                }, status=400)
        return redirect("configuration")
    return render(request, "panel/configuration.html", {
        "preferences": preferences,
        "payment_form": payment_form,
        "profile_bucket": settings.SUPABASE_PROFILE_BUCKET,
        "patient_bucket": settings.SUPABASE_PATIENT_BUCKET,
        "payment_bucket": settings.SUPABASE_PAYMENT_BUCKET,
        "bank_bucket": settings.SUPABASE_BANK_BUCKET,
    })
