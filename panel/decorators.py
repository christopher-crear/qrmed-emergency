from functools import wraps

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect

from .models import Patient, Profile


def _session_profile(request):
    if hasattr(request, "_qrmed_session_profile"):
        return request._qrmed_session_profile
    user_id = request.session.get("supabase_user_id")
    if not user_id:
        request._qrmed_session_profile = None
        return None
    try:
        profile = Profile.objects.get(id=user_id)
    except (Profile.DoesNotExist, ValueError):
        profile = None
    request._qrmed_session_profile = profile
    return profile


def authenticated_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        profile = _session_profile(request)
        if not profile or not profile.is_active:
            request.session.flush()
            messages.error(request, "Inicia sesión con una cuenta activa.")
            return redirect("login")
        request.account_profile = profile
        return view(request, *args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        profile = _session_profile(request)
        if not profile:
            request.session.flush()
            messages.error(request, "Tu cuenta no tiene un perfil válido.")
            return redirect("login")
        role = (profile.role or "").lower()
        if role not in {"admin", "administrador"} or not profile.is_active:
            request.session.flush()
            messages.error(request, "Esta cuenta no tiene acceso al panel administrativo.")
            return redirect("login")
        request.admin_profile = profile
        return view(request, *args, **kwargs)

    return wrapped


def patient_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        profile = _session_profile(request)
        if not profile:
            request.session.flush()
            messages.error(request, "Tu cuenta no tiene un perfil válido.")
            return redirect("login")
        role = (profile.role or "").lower()
        if role not in {"user", "usuario", "patient", "paciente"} or not profile.is_active:
            request.session.flush()
            messages.error(request, "Esta cuenta no tiene acceso al panel de paciente.")
            return redirect("login")
        patient = Patient.objects.filter(
            Q(owner_id=profile.id) | Q(id=profile.id)
        ).order_by("-created_at").first()
        if not patient:
            request.session.flush()
            messages.error(request, "Tu cuenta todavía no tiene una ficha médica asociada.")
            return redirect("login")
        request.user_profile = profile
        request.patient = patient
        request.account_profile = profile
        return view(request, *args, **kwargs)

    return wrapped
