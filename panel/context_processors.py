from django.conf import settings
from django.urls import reverse

from django.db.models import Q

from .models import Patient, Profile
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
    return {
        "admin_profile": profile if is_admin else None,
        "session_profile": profile,
        "session_patient": patient,
        "is_admin_session": is_admin,
        "admin_email": request.session.get("supabase_email", ""),
        "admin_avatar_url": avatar_url,
        "session_avatar_url": avatar_url,
        "cart_count": cart_count,
        "demo_mode": settings.DEMO_MODE,
    }
