import io
import uuid
from decimal import Decimal

import qrcode
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import patient_required
from .forms import PatientEmergencyForm, PatientMedicalForm, PatientPersonalForm, ProfileForm
from .models import BankAccount, Order, OrderItem, PaymentSetting, Product
from .services import (
    SupabaseError, sign_in, storage_signed_url, update_password, upload_file,
    versioned_media_url,
)


def _patient_orders(request):
    return Order.objects.filter(
        user_id__in={request.user_profile.id, request.patient.id}
    ).order_by("-created_at")


def _order_rows(orders):
    order_list = list(orders)
    items = list(OrderItem.objects.filter(order_id__in=[item.id for item in order_list]))
    product_map = {
        str(product.id): product
        for product in Product.objects.filter(id__in=[item.product_id for item in items if item.product_id])
    }
    grouped = {}
    for item in items:
        grouped.setdefault(str(item.order_id), []).append(item)
    rows = []
    for order in order_list:
        order_items = grouped.get(str(order.id), [])
        first_item = order_items[0] if order_items else None
        rows.append({
            "order": order,
            "items": order_items,
            "first_item": first_item,
            "product": product_map.get(str(first_item.product_id)) if first_item and first_item.product_id else None,
        })
    return rows


def _cart_data(request):
    value = request.session.get("patient_cart", {})
    return value if isinstance(value, dict) else {}


def _cart_context(request):
    cart_data = _cart_data(request)
    products = Product.objects.filter(id__in=list(cart_data), is_active=True)
    rows = []
    total = Decimal("0")
    for product in products:
        saved = cart_data.get(str(product.id), {})
        quantity = max(1, min(int(saved.get("quantity", 1)), max(product.stock, 1)))
        line_total = (product.price or Decimal("0")) * quantity
        total += line_total
        rows.append({
            "product": product,
            "quantity": quantity,
            "color": saved.get("color", ""),
            "size": saved.get("size", ""),
            "line_total": line_total,
        })
    return {"cart_rows": rows, "cart_total": total, "cart_count": sum(row["quantity"] for row in rows)}


@patient_required
def dashboard(request):
    orders = _patient_orders(request)
    stats = orders.aggregate(
        total=Count("id"),
        in_progress=Count("id", filter=~Q(status__in=["delivered", "cancelled"])),
    )
    context = {
        "orders_count": stats["total"],
        "orders_in_progress": stats["in_progress"],
        "recent_orders": _order_rows(orders[:4]),
    }
    return render(request, "panel/patient_dashboard.html", context)


@patient_required
def global_search(request):
    query = request.GET.get("q", "").strip()
    results = []
    if len(query) >= 2:
        for product in Product.objects.filter(name__icontains=query, is_active=True)[:6]:
            results.append({"title": product.name, "subtitle": "Pulsera disponible", "url": reverse("patient_store") + f"?q={query}"})
        for order in _patient_orders(request).filter(order_number__icontains=query)[:6]:
            results.append({"title": order.order_number, "subtitle": "Mi pedido", "url": reverse("patient_order_detail", kwargs={"order_id": order.id})})
    return JsonResponse({"results": results})


@patient_required
def credential(request):
    return render(request, "panel/patient_credential.html", {
        "issue_date": request.patient.created_at,
    })


@patient_required
def credential_qr(request):
    public_url = request.build_absolute_uri(reverse("public_patient", kwargs={"token": request.patient.qr_token}))
    cache_key = f"patient-credential-qr:{request.patient.qr_token}:{public_url}"
    image_bytes = cache.get(cache_key)
    if image_bytes is None:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(public_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#183b62", back_color="white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        image_bytes = output.getvalue()
        cache.set(cache_key, image_bytes, timeout=3600)
    response = HttpResponse(image_bytes, content_type="image/png")
    response["Cache-Control"] = "private, max-age=3600"
    return response


@patient_required
def store(request):
    query = request.GET.get("q", "").strip()
    products = Product.objects.filter(is_active=True, stock__gt=0)
    if len(query) >= 2:
        products = products.filter(Q(name__icontains=query) | Q(description__icontains=query))
    context = _cart_context(request)
    context.update({"products": products, "q": query})
    return render(request, "panel/patient_store.html", context)


@patient_required
@require_POST
def cart_add(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    cart_data = _cart_data(request)
    quantity = max(1, min(int(request.POST.get("quantity", 1)), max(product.stock, 1)))
    colors = product.colors or []
    sizes = product.sizes or []
    color = request.POST.get("color", "").strip()
    size = request.POST.get("size", "").strip()
    if colors and color not in colors:
        color = colors[0]
    if sizes and size not in sizes:
        size = sizes[0]
    cart_data[str(product.id)] = {"quantity": quantity, "color": color, "size": size}
    request.session["patient_cart"] = cart_data
    request.session.modified = True
    messages.success(request, f"{product.name} se agregó al carrito.")
    return redirect(request.POST.get("next") or "patient_store")


@patient_required
@require_POST
def cart_remove(request, product_id):
    cart_data = _cart_data(request)
    cart_data.pop(str(product_id), None)
    request.session["patient_cart"] = cart_data
    request.session.modified = True
    messages.success(request, "Producto retirado del carrito.")
    return redirect("patient_cart")


@patient_required
def cart(request):
    return render(request, "panel/patient_cart.html", _cart_context(request))


@patient_required
def checkout(request):
    cart_context = _cart_context(request)
    if not cart_context["cart_rows"]:
        messages.info(request, "Agrega al menos un producto antes de finalizar.")
        return redirect("patient_store")
    payment = PaymentSetting.objects.first()
    try:
        bank_accounts = list(BankAccount.objects.filter(is_visible=True).order_by("display_order", "bank_name"))
    except DatabaseError:
        bank_accounts = []
    for bank in bank_accounts:
        bank.logo_url = reverse("bank_asset_image", kwargs={"bank_id": bank.id, "kind": "logo"}) if bank.logo_path else ""
        bank.qr_url = reverse("bank_asset_image", kwargs={"bank_id": bank.id, "kind": "qr"}) if bank.qr_path else ""
    selected_bank_id = request.POST.get("bank_account", "").strip()
    if not selected_bank_id and bank_accounts:
        selected_bank_id = str(bank_accounts[0].id)
    selected_bank = next((bank for bank in bank_accounts if str(bank.id) == selected_bank_id), None)
    if bank_accounts and selected_bank is None:
        selected_bank = bank_accounts[0]
        selected_bank_id = str(selected_bank.id)
    shipping = {
        "name": request.POST.get("shipping_name", request.patient.full_name or "").strip(),
        "address": request.POST.get("shipping_address", request.patient.address or "").strip(),
        "city": request.POST.get("shipping_city", request.patient.city or "").strip(),
        "postal": request.POST.get("shipping_postal", "").strip(),
        "phone": request.POST.get("shipping_phone", request.patient.phone or "").strip(),
    }
    selected_method = request.POST.get("payment_method", "transfer")
    if request.method == "POST":
        proof = request.FILES.get("proof")
        method = selected_method
        allowed_types = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
        if not all((shipping["name"], shipping["address"], shipping["city"], shipping["phone"])):
            messages.error(request, "Completa los datos obligatorios de envío.")
        elif method not in {"transfer", "deposit"}:
            messages.error(request, "Selecciona un método de pago válido.")
        elif not proof:
            messages.error(request, "Adjunta el comprobante de pago.")
        elif proof.size > 10 * 1024 * 1024 or proof.content_type not in allowed_types:
            messages.error(request, "El comprobante debe ser JPG, PNG, WebP o PDF y no superar 10 MB.")
        else:
            now = timezone.now()
            order_id = uuid.uuid4()
            try:
                proof_path = upload_file(
                    proof, settings.SUPABASE_PAYMENT_BUCKET,
                    f"{request.user_profile.id}/{order_id}", "proof-",
                    request.session.get("supabase_access_token", ""),
                )
            except SupabaseError as exc:
                messages.error(request, str(exc))
            else:
                with transaction.atomic():
                    order = Order(
                        id=order_id,
                        user_id=request.user_profile.id,
                        order_number=f"ORD-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
                        total=cart_context["cart_total"], subtotal=cart_context["cart_total"],
                        discount_amount=Decimal("0"), status="pending", payment_method=method,
                        payment_proof_path=proof_path,
                        shipping_address=shipping["address"], shipping_name=shipping["name"],
                        shipping_city=shipping["city"], shipping_postal=shipping["postal"],
                        shipping_phone=shipping["phone"], created_at=now, updated_at=now,
                    )
                    order.save(force_insert=True)
                    for row in cart_context["cart_rows"]:
                        OrderItem(
                            id=uuid.uuid4(), order_id=order.id, product_id=row["product"].id,
                            quantity=row["quantity"], unit_price=row["product"].price,
                            selected_color=row["color"], selected_size=row["size"],
                        ).save(force_insert=True)
                        if row["product"].stock >= row["quantity"]:
                            row["product"].stock -= row["quantity"]
                            row["product"].updated_at = now
                            row["product"].save(update_fields=["stock", "updated_at"])
                request.session["patient_cart"] = {}
                request.session.modified = True
                messages.success(request, f"Pedido {order.order_number} enviado para validar el pago.")
                return redirect("patient_checkout_success", order_id=order.id)
    context = cart_context
    context.update({
        "payment": payment,
        "bank_accounts": bank_accounts,
        "selected_bank": selected_bank,
        "selected_bank_id": selected_bank_id,
        "shipping": shipping,
        "selected_payment_method": selected_method,
    })
    return render(request, "panel/patient_checkout.html", context)


@patient_required
def checkout_success(request, order_id):
    order = get_object_or_404(_patient_orders(request), id=order_id)
    row = _order_rows([order])[0]
    return render(request, "panel/patient_checkout_success.html", row)


@patient_required
def medical_record(request, step):
    form_classes = {1: PatientPersonalForm, 2: PatientMedicalForm, 3: PatientEmergencyForm}
    if step not in form_classes:
        return redirect("patient_medical_record", step=1)
    form = form_classes[step](request.POST or None, request.FILES or None, instance=request.patient)
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
                return render(request, "panel/patient_medical_record.html", {"form": form, "step": step})
        patient.updated_at = timezone.now()
        update_fields = list(form._meta.fields) + ["updated_at"]
        if step == 1 and request.FILES.get("photo"):
            update_fields.append("photo_path")
        try:
            patient.save(update_fields=list(dict.fromkeys(update_fields)))
        except IntegrityError as exc:
            if "patients_sex_check" not in str(exc):
                raise
            form.add_error(
                "sex" if "sex" in form.fields else None,
                "Selecciona nuevamente el sexo.",
            )
            return render(request, "panel/patient_medical_record.html", {
                "form": form, "step": step,
            })
        if step < 3:
            return redirect("patient_medical_record", step=step + 1)
        messages.success(request, "Tu ficha médica se actualizó correctamente.")
        return redirect("patient_dashboard")
    return render(request, "panel/patient_medical_record.html", {"form": form, "step": step})


@patient_required
def orders(request):
    query = request.GET.get("q", "").strip()
    base_queryset = _patient_orders(request)
    orders_total = base_queryset.count()
    queryset = base_queryset
    if len(query) >= 2:
        queryset = queryset.filter(order_number__icontains=query)
    return render(request, "panel/patient_orders.html", {
        "rows": _order_rows(queryset),
        "orders_total": orders_total,
        "q": query,
    })


@patient_required
def order_detail(request, order_id):
    order = get_object_or_404(_patient_orders(request), id=order_id)
    row = _order_rows([order])[0]
    row["proof_url"] = storage_signed_url(
        order.payment_proof_path, settings.SUPABASE_PAYMENT_BUCKET,
        access_token=request.session.get("supabase_access_token", ""),
    )
    query = request.GET.get("q", "").strip()
    list_queryset = _patient_orders(request)
    row["list_orders_total"] = list_queryset.count()
    if len(query) >= 2:
        list_queryset = list_queryset.filter(order_number__icontains=query)
    row["list_rows"] = _order_rows(list_queryset)
    row["list_q"] = query
    return render(request, "panel/patient_order_detail.html", row)


@patient_required
def profile(request):
    order_count = _patient_orders(request).count()
    profile_obj = request.user_profile
    patient = request.patient
    return render(request, "panel/patient_profile.html", {
        "profile_form": ProfileForm(instance=profile_obj),
        "order_count": order_count,
        "profile_cover_url": versioned_media_url(
            reverse("profile_cover_image", kwargs={"profile_id": profile_obj.id}),
            profile_obj.updated_at,
        ) if profile_obj.cover_path else "",
        "profile_avatar_url": (
            versioned_media_url(
                reverse("profile_avatar_image", kwargs={"profile_id": profile_obj.id}),
                profile_obj.updated_at,
            )
            if profile_obj.avatar_path
            else versioned_media_url(
                reverse("patient_photo_image", kwargs={"patient_id": patient.id}),
                patient.updated_at,
            )
            if patient.photo_path
            else ""
        ),
        "profile_display_name": profile_obj.full_name or patient.full_name,
        "profile_email": patient.email or request.session.get("supabase_email", ""),
        "profile_phone": profile_obj.phone or patient.phone or "",
        "profile_city": profile_obj.city or patient.city or "",
        "profile_specialty": profile_obj.specialty or "Paciente",
        "profile_specialty_label": "Ocupación",
        "profile_role_label": "Paciente",
        "profile_role_icon": "heart-pulse",
        "profile_edit_action": reverse("patient_profile_edit"),
        "profile_password_action": reverse("patient_password_update"),
        "profile_metric_primary_label": "Fichas registradas",
        "profile_metric_primary": 1,
        "profile_metric_qr_label": "QR generados",
        "profile_metric_qr": 1,
        "profile_metric_orders": order_count,
        "profile_member_since": profile_obj.created_at,
        "profile_danger_text": "La eliminación de cuentas requiere validación del administrador para proteger tu historial médico.",
    })


@patient_required
@require_POST
def profile_edit(request):
    form = ProfileForm(request.POST, request.FILES, instance=request.user_profile)
    if form.is_valid():
        profile_obj = form.save(commit=False)
        try:
            if request.FILES.get("avatar"):
                profile_obj.avatar_path = upload_file(
                    request.FILES["avatar"], settings.SUPABASE_PROFILE_BUCKET,
                    str(profile_obj.id), "avatar-", request.session.get("supabase_access_token", ""),
                )
            if request.FILES.get("cover"):
                profile_obj.cover_path = upload_file(
                    request.FILES["cover"], settings.SUPABASE_PROFILE_BUCKET,
                    str(profile_obj.id), "cover-", request.session.get("supabase_access_token", ""),
                )
        except SupabaseError as exc:
            messages.error(request, str(exc))
        else:
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
    return redirect("patient_profile")


@patient_required
@require_POST
def password_update(request):
    current = request.POST.get("current_password", "")
    new = request.POST.get("new_password", "")
    confirmation = request.POST.get("confirm_password", "")
    if len(new) < 8:
        messages.error(request, "La contraseña nueva debe tener al menos 8 caracteres.")
    elif new != confirmation:
        messages.error(request, "Las contraseñas no coinciden.")
    else:
        try:
            auth = sign_in(request.session.get("supabase_email", ""), current)
            update_password(auth.get("access_token", ""), new)
            request.session["supabase_access_token"] = auth.get("access_token", "")
            messages.success(request, "Contraseña actualizada.")
        except SupabaseError as exc:
            messages.error(request, str(exc))
    return redirect("patient_profile")


@patient_required
def configuration(request):
    preferences = request.user_profile.preferences or {}
    if request.method == "POST":
        boolean_keys = ["order_updates", "qr_activity", "email_news", "system_alerts", "public_profile", "analytics"]
        preferences = {
            "language": request.POST.get("language", "es"),
            "theme": request.POST.get("theme", "light"),
            **{key: request.POST.get(key) == "on" for key in boolean_keys},
        }
        request.user_profile.preferences = preferences
        request.user_profile.updated_at = timezone.now()
        request.user_profile.save()
        messages.success(request, "Preferencias guardadas.")
        return redirect("patient_configuration")
    return render(request, "panel/patient_configuration.html", {"preferences": preferences})
