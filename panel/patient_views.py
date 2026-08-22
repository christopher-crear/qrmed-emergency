import io
import secrets
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
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .decorators import patient_required
from .forms import PatientEmergencyForm, PatientMedicalForm, PatientPersonalForm, ProfileForm
from .credential_pdf import build_credential_pdf
from .models import BankAccount, DiscountCampaign, DiscountTicket, Invoice, Order, OrderItem, PaymentSetting, Product
from .pagination import paginate_items
from .services import (
    SupabaseError, sign_in, storage_image_bytes, storage_signed_url, update_password, upload_file,
    versioned_media_url,
)


def _patient_orders(request):
    return Order.objects.filter(
        user_id__in={request.user_profile.id, request.patient.id}
    ).order_by("-created_at")


def _delivery_code():
    for _ in range(25):
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not Order.objects.filter(tracking_number=code).exists():
            return code
    return f"{secrets.randbelow(10_000_000):07d}"


def _ensure_delivery_code(order):
    if not str(order.tracking_number or "").strip():
        order.tracking_number = _delivery_code()
        order.updated_at = timezone.now()
        order.save(update_fields=["tracking_number", "updated_at"])


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
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            continue
        saved = dict(item)
        saved.setdefault("product_id", str(key).split(":", 1)[0])
        normalized[str(key)] = saved
    return normalized


def _cart_line_key(product_id, color, size):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"qrmed:{product_id}:{color}:{size}").hex


class DiscountClaimError(Exception):
    pass


def _campaign_is_open(campaign, now=None):
    now = now or timezone.now()
    return bool(
        campaign.is_active
        and (not campaign.starts_at or campaign.starts_at <= now)
        and (not campaign.expires_at or campaign.expires_at > now)
    )


def _discount_amount(campaign, subtotal):
    subtotal = Decimal(subtotal or 0)
    if subtotal < Decimal(campaign.min_order_amount or 0):
        return Decimal("0")
    value = Decimal(campaign.discount_value or 0)
    if campaign.discount_type == "percentage":
        amount = subtotal * value / Decimal("100")
    else:
        amount = value
    return min(subtotal, amount).quantize(Decimal("0.01"))


def _selected_discount(request, subtotal):
    ticket_id = request.session.get("discount_ticket_id")
    if not ticket_id:
        return None
    try:
        ticket = DiscountTicket.objects.filter(
            id=ticket_id, user_id=request.user_profile.id, used_at__isnull=True
        ).first()
        campaign = DiscountCampaign.objects.filter(id=ticket.campaign_id).first() if ticket else None
    except (DatabaseError, ValueError):
        return None
    if not ticket or not campaign or not _campaign_is_open(campaign):
        request.session.pop("discount_ticket_id", None)
        request.session.modified = True
        return None
    amount = _discount_amount(campaign, subtotal)
    if amount <= 0:
        return {"ticket": ticket, "campaign": campaign, "amount": amount, "eligible": False}
    return {"ticket": ticket, "campaign": campaign, "amount": amount, "eligible": True}


def _claim_campaign(request, campaign):
    now = timezone.now()
    if not _campaign_is_open(campaign, now):
        raise DiscountClaimError("Este ticket todavía no está disponible o ya venció.")
    existing = DiscountTicket.objects.filter(
        campaign_id=campaign.id, user_id=request.user_profile.id
    ).first()
    if existing:
        if existing.used_at:
            raise DiscountClaimError("Ya utilizaste el ticket de esta campaña.")
        return existing, False
    claimed = DiscountTicket.objects.filter(campaign_id=campaign.id).count()
    if claimed >= campaign.max_claims:
        raise DiscountClaimError("Los tickets de esta campaña se agotaron.")
    ticket = DiscountTicket(
        id=uuid.uuid4(), campaign_id=campaign.id, user_id=request.user_profile.id,
        claimed_at=now,
    )
    ticket.save(force_insert=True)
    return ticket, True


def _available_discounts(request):
    now = timezone.now()
    try:
        campaigns = list(DiscountCampaign.objects.filter(is_active=True).order_by("-created_at"))
        claimed_ids = set(DiscountTicket.objects.filter(
            user_id=request.user_profile.id
        ).values_list("campaign_id", flat=True))
        campaign_ids = [campaign.id for campaign in campaigns]
        claim_counts = {
            str(row["campaign_id"]): row["total"]
            for row in DiscountTicket.objects.filter(campaign_id__in=campaign_ids)
            .values("campaign_id").annotate(total=Count("id"))
        }
        result = []
        for campaign in campaigns:
            if not _campaign_is_open(campaign, now) or campaign.id in claimed_ids:
                continue
            campaign.claimed_count = claim_counts.get(str(campaign.id), 0)
            campaign.remaining_count = max(0, campaign.max_claims - campaign.claimed_count)
            if campaign.remaining_count:
                result.append(campaign)
        return result
    except DatabaseError:
        return []


def _cart_context(request):
    cart_data = _cart_data(request)
    product_ids = {str(item.get("product_id") or "") for item in cart_data.values()}
    products = {
        str(product.id): product
        for product in Product.objects.only(
            "id", "name", "description", "price", "stock", "image_url", "colors", "sizes", "is_active"
        ).filter(id__in=product_ids, is_active=True)
    }
    rows = []
    total = Decimal("0")
    cleaned_cart = {}
    remaining_stock = {product_id: max(product.stock, 0) for product_id, product in products.items()}
    for line_key, saved in cart_data.items():
        product = products.get(str(saved.get("product_id") or ""))
        if not product:
            continue
        try:
            requested_quantity = int(saved.get("quantity", 1))
        except (TypeError, ValueError):
            requested_quantity = 1
        available = remaining_stock.get(str(product.id), 0)
        if available <= 0:
            continue
        quantity = max(1, min(requested_quantity, available))
        remaining_stock[str(product.id)] = available - quantity
        line_total = (product.price or Decimal("0")) * quantity
        total += line_total
        cleaned_cart[line_key] = {**saved, "product_id": str(product.id), "quantity": quantity}
        rows.append({
            "line_key": line_key,
            "product": product,
            "quantity": quantity,
            "color": saved.get("color", ""),
            "size": saved.get("size", ""),
            "line_total": line_total,
        })
    if cleaned_cart != cart_data:
        request.session["patient_cart"] = cleaned_cart
        request.session.modified = True
    selected_discount = _selected_discount(request, total)
    discount_amount = selected_discount["amount"] if selected_discount and selected_discount["eligible"] else Decimal("0")
    return {
        "cart_rows": rows,
        "cart_subtotal": total,
        "cart_total": max(Decimal("0"), total - discount_amount),
        "cart_count": sum(row["quantity"] for row in rows),
        "discount_amount": discount_amount,
        "selected_discount": selected_discount,
    }


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
    modules = [
        ("Inicio", "Resumen de mi cuenta", "patient_dashboard"),
        ("Mi credencial", "Código QR de emergencia", "patient_credential"),
        ("Comprar pulsera", "Catálogo de productos", "patient_store"),
        ("Carrito", "Productos seleccionados", "patient_cart"),
        ("Mis descuentos", "Tickets y cupones disponibles", "patient_discounts"),
        ("Mi ficha médica", "Datos médicos y contactos", "patient_medical_record"),
        ("Mis pedidos", "Seguimiento y código de entrega", "patient_orders"),
        ("Buzón", "Facturas y documentos recibidos", "patient_mailbox"),
        ("Pedidos entregados", "Historial de compras finalizadas", "patient_delivered_history"),
        ("Perfil", "Datos de mi cuenta", "patient_profile"),
        ("Configuración", "Preferencias y notificaciones", "patient_configuration"),
    ]
    results = []
    for title, subtitle, url_name in modules:
        if query and query.casefold() not in f"{title} {subtitle}".casefold():
            continue
        url = reverse(url_name, kwargs={"step": 1}) if url_name == "patient_medical_record" else reverse(url_name)
        results.append({"title": title, "subtitle": subtitle, "url": url})
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


def _credential_qr_bytes(request):
    public_url = request.build_absolute_uri(reverse("public_patient", kwargs={"token": request.patient.qr_token}))
    cache_key = f"patient-credential-qr:{request.patient.qr_token}:{public_url}"
    image_bytes = cache.get(cache_key)
    if image_bytes is None:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(public_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#0B2A57", back_color="white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        image_bytes = output.getvalue()
        cache.set(cache_key, image_bytes, timeout=3600)
    return image_bytes


@patient_required
def credential_qr(request):
    image_bytes = _credential_qr_bytes(request)
    response = HttpResponse(image_bytes, content_type="image/png")
    response["Cache-Control"] = "private, max-age=3600"
    return response


@patient_required
def credential_print(request):
    """Vista limpia de impresión con frente y reverso de la credencial."""
    return render(request, "panel/patient_credential_print.html", {
        "issue_date": request.patient.created_at,
    })


@patient_required
def credential_pdf(request):
    """Genera y descarga el frente y reverso de la credencial médica en PDF."""
    qr_bytes = _credential_qr_bytes(request)
    photo_bytes = None
    if request.patient.photo_path:
        image = storage_image_bytes(
            settings.SUPABASE_PATIENT_BUCKET,
            request.patient.photo_path,
            identifiers=(request.patient.id, request.patient.owner_id),
            access_token=request.session.get("supabase_access_token", ""),
        )
        if image:
            photo_bytes = image[0]

    pdf_bytes = build_credential_pdf(
        request.patient,
        qr_bytes=qr_bytes,
        photo_bytes=photo_bytes,
    )
    filename = slugify(request.patient.full_name) or "paciente"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    disposition = "inline" if request.GET.get("preview") == "1" else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="credencial-qrmed-{filename}.pdf"'
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@patient_required
def store(request):
    query = request.GET.get("q", "").strip()
    products = Product.objects.filter(is_active=True, stock__gt=0)
    if len(query) >= 2:
        products = products.filter(Q(name__icontains=query) | Q(description__icontains=query))
    pagination = paginate_items(request, products)
    context = _cart_context(request)
    context.update({
        "products": pagination.pop("items"), "q": query,
        "available_discounts": _available_discounts(request),
        **pagination,
    })
    return render(request, "panel/patient_store.html", context)


@patient_required
@require_POST
def cart_add(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True, stock__gt=0)
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
    line_key = _cart_line_key(product.id, color, size)
    existing_quantity = int(cart_data.get(line_key, {}).get("quantity", 0) or 0)
    reserved_other = 0
    for key, item in cart_data.items():
        if key == line_key or str(item.get("product_id")) != str(product.id):
            continue
        try:
            reserved_other += max(0, int(item.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            continue
    available_for_line = max(0, product.stock - reserved_other)
    if available_for_line <= 0:
        messages.error(request, "Ya agregaste todas las unidades disponibles de este producto.")
        return redirect(request.POST.get("next") or "patient_store")
    cart_data[line_key] = {
        "product_id": str(product.id),
        "quantity": min(available_for_line, existing_quantity + quantity),
        "color": color, "size": size,
    }
    request.session["patient_cart"] = cart_data
    request.session.modified = True
    messages.success(request, f"{product.name} se agregó al carrito.")
    return redirect(request.POST.get("next") or "patient_store")


@patient_required
@require_POST
def cart_remove(request, line_key):
    cart_data = _cart_data(request)
    cart_data.pop(str(line_key), None)
    request.session["patient_cart"] = cart_data
    request.session.modified = True
    messages.success(request, "Producto retirado del carrito.")
    return redirect("patient_cart")


@patient_required
def cart(request):
    context = _cart_context(request)
    try:
        tickets = list(DiscountTicket.objects.filter(
            user_id=request.user_profile.id, used_at__isnull=True
        ))
        campaigns = {str(item.id): item for item in DiscountCampaign.objects.filter(
            id__in=[ticket.campaign_id for ticket in tickets]
        )}
        context["wallet_tickets"] = [
            {"ticket": ticket, "campaign": campaigns.get(str(ticket.campaign_id))}
            for ticket in tickets if campaigns.get(str(ticket.campaign_id))
        ]
    except DatabaseError:
        context["wallet_tickets"] = []
    return render(request, "panel/patient_cart.html", context)


@patient_required
@require_POST
def cart_discount_apply(request):
    code = str(request.POST.get("discount_code") or "").strip().upper()
    if not code:
        messages.error(request, "Escribe un código de descuento.")
        return redirect("patient_cart")
    try:
        with transaction.atomic():
            campaign = DiscountCampaign.objects.select_for_update().filter(code__iexact=code).first()
            if not campaign:
                raise DiscountClaimError("El código ingresado no existe.")
            ticket, created = _claim_campaign(request, campaign)
            subtotal = _cart_context(request)["cart_subtotal"]
            if subtotal < Decimal(campaign.min_order_amount or 0):
                raise DiscountClaimError(f"Este ticket requiere una compra mínima de ${campaign.min_order_amount}.")
            request.session["discount_ticket_id"] = str(ticket.id)
            request.session.modified = True
    except (DiscountClaimError, DatabaseError, IntegrityError) as exc:
        messages.error(request, str(exc) if str(exc) else "No se pudo aplicar el ticket.")
    else:
        messages.success(request, "Ticket reclamado y aplicado al carrito." if created else "Descuento aplicado al carrito.")
    return redirect("patient_cart")


@patient_required
@require_POST
def cart_discount_remove(request):
    request.session.pop("discount_ticket_id", None)
    request.session.modified = True
    messages.info(request, "Descuento retirado del carrito.")
    return redirect("patient_cart")


@patient_required
@require_POST
def discount_claim(request, campaign_id):
    try:
        with transaction.atomic():
            campaign = DiscountCampaign.objects.select_for_update().get(id=campaign_id)
            ticket, created = _claim_campaign(request, campaign)
    except DiscountCampaign.DoesNotExist:
        messages.error(request, "La campaña ya no existe.")
    except (DiscountClaimError, DatabaseError, IntegrityError) as exc:
        messages.error(request, str(exc) if str(exc) else "No se pudo reclamar el ticket.")
    else:
        messages.success(request, "¡Conseguiste el ticket! Ya está en tu billetera." if created else "Este ticket ya está en tu billetera.")
    return redirect("patient_discounts")


@patient_required
def discounts(request):
    try:
        tickets = list(DiscountTicket.objects.filter(user_id=request.user_profile.id))
        campaigns = {str(item.id): item for item in DiscountCampaign.objects.filter(
            id__in=[ticket.campaign_id for ticket in tickets]
        )}
        rows = []
        for ticket in tickets:
            campaign = campaigns.get(str(ticket.campaign_id))
            if not campaign:
                continue
            rows.append({
                "ticket": ticket, "campaign": campaign,
                "is_available": not ticket.used_at and _campaign_is_open(campaign),
            })
        schema_ready = True
    except DatabaseError:
        rows, schema_ready = [], False
    pagination = paginate_items(request, rows)
    return render(request, "panel/patient_discounts.html", {
        "ticket_rows": pagination.pop("items"),
        "available_campaigns": _available_discounts(request) if schema_ready else [],
        "schema_ready": schema_ready,
        **pagination,
    })


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
                    discount_ticket = None
                    discount_campaign = None
                    discount_amount = Decimal("0")
                    selected_discount = cart_context.get("selected_discount")
                    if selected_discount and selected_discount.get("eligible"):
                        discount_ticket = DiscountTicket.objects.select_for_update().filter(
                            id=selected_discount["ticket"].id,
                            user_id=request.user_profile.id,
                            used_at__isnull=True,
                        ).first()
                        if discount_ticket:
                            discount_campaign = DiscountCampaign.objects.select_for_update().filter(
                                id=discount_ticket.campaign_id
                            ).first()
                        if not discount_ticket or not discount_campaign or not _campaign_is_open(discount_campaign, now):
                            request.session.pop("discount_ticket_id", None)
                            request.session.modified = True
                            messages.error(request, "El ticket dejó de estar disponible. Vuelve al carrito y selecciona otro.")
                            return redirect("patient_cart")
                        discount_amount = _discount_amount(discount_campaign, cart_context["cart_subtotal"])
                    final_total = max(Decimal("0"), cart_context["cart_subtotal"] - discount_amount)
                    order = Order(
                        id=order_id,
                        user_id=request.user_profile.id,
                        order_number=f"ORD-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
                        total=final_total, subtotal=cart_context["cart_subtotal"],
                        discount_amount=discount_amount,
                        discount_code=discount_campaign.code if discount_campaign else None,
                        status="pending", payment_method=method,
                        payment_proof_path=proof_path,
                        shipping_address=shipping["address"], shipping_name=shipping["name"],
                        shipping_city=shipping["city"], shipping_postal=shipping["postal"],
                        shipping_phone=shipping["phone"], tracking_number=_delivery_code(),
                        created_at=now, updated_at=now,
                    )
                    order.save(force_insert=True)
                    if discount_ticket:
                        discount_ticket.used_at = now
                        discount_ticket.order_id = order.id
                        discount_ticket.save(update_fields=["used_at", "order_id"])
                        cache.delete(f"qrmed-ticket-count:{request.user_profile.id}")
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
                request.session.pop("discount_ticket_id", None)
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
    _ensure_delivery_code(order)
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
        preferences = request.user_profile.preferences if isinstance(request.user_profile.preferences, dict) else {}
        preferences = {**preferences, "medical_profile_pending": False}
        request.user_profile.preferences = preferences
        request.user_profile.updated_at = timezone.now()
        request.user_profile.save(update_fields=["preferences", "updated_at"])
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
    pagination = paginate_items(request, queryset)
    return render(request, "panel/patient_orders.html", {
        "rows": _order_rows(pagination.pop("items")),
        "orders_total": orders_total,
        "q": query,
        **pagination,
    })


@patient_required
def order_detail(request, order_id):
    order = get_object_or_404(_patient_orders(request), id=order_id)
    _ensure_delivery_code(order)
    row = _order_rows([order])[0]
    try:
        row["invoice"] = Invoice.objects.filter(order_id=order.id).first()
    except DatabaseError:
        row["invoice"] = None
    row["proof_url"] = storage_signed_url(
        order.payment_proof_path, settings.SUPABASE_PAYMENT_BUCKET,
        access_token=request.session.get("supabase_access_token", ""),
    )
    query = request.GET.get("q", "").strip()
    list_queryset = _patient_orders(request)
    row["list_orders_total"] = list_queryset.count()
    if len(query) >= 2:
        list_queryset = list_queryset.filter(order_number__icontains=query)
    pagination = paginate_items(request, list_queryset)
    row["list_rows"] = _order_rows(pagination.pop("items"))
    row["list_q"] = query
    row.update({f"list_{key}": value for key, value in pagination.items()})
    return render(request, "panel/patient_order_detail.html", row)


@patient_required
def mailbox(request):
    try:
        invoices = list(Invoice.objects.filter(
            user_id__in={request.user_profile.id, request.patient.id},
            sent_at__isnull=False,
        ).order_by("-sent_at"))
        orders = {
            str(order.id): order
            for order in Order.objects.filter(id__in=[invoice.order_id for invoice in invoices])
        }
        rows = [
            {"invoice": invoice, "order": orders.get(str(invoice.order_id))}
            for invoice in invoices if orders.get(str(invoice.order_id))
        ]
        schema_ready = True
    except DatabaseError:
        rows, schema_ready = [], False
    pagination = paginate_items(request, rows)
    return render(request, "panel/patient_mailbox.html", {
        "invoice_rows": pagination.pop("items"), "schema_ready": schema_ready,
        **pagination,
    })


@patient_required
def delivered_history(request):
    query = request.GET.get("q", "").strip()
    queryset = _patient_orders(request).filter(status="delivered")
    total = queryset.count()
    if len(query) >= 2:
        queryset = queryset.filter(order_number__icontains=query)
    pagination = paginate_items(request, queryset)
    return render(request, "panel/patient_delivered_history.html", {
        "rows": _order_rows(pagination.pop("items")),
        "orders_total": total, "q": query, **pagination,
    })


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
            **preferences,
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
