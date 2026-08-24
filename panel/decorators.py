from functools import wraps
import uuid

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect
from django.utils import timezone

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
        if role not in {"user", "usuario", "patient", "paciente", "client", "cliente"} or not profile.is_active:
            request.session.flush()
            messages.error(request, "Esta cuenta no tiene acceso al panel de paciente.")
            return redirect("login")
        patient = Patient.objects.filter(
            Q(owner_id=profile.id) | Q(id=profile.id)
        ).order_by("-created_at").first()
        if not patient:
            # El alta de Auth/perfil y la ficha médica son etapas distintas. La
            # instancia permanece sin guardar hasta validar el primer formulario,
            # evitando violar restricciones con datos provisionales.
            if request.resolver_match and request.resolver_match.url_name == "patient_medical_record":
                name_parts = str(profile.full_name or "").strip().split(maxsplit=1)
                patient = Patient(
                    id=uuid.uuid4(), owner_id=profile.id,
                    first_name=name_parts[0] if name_parts else "",
                    last_name=name_parts[1] if len(name_parts) > 1 else "",
                    email=request.session.get("supabase_email", ""),
                    phone=profile.phone or "", qr_token=uuid.uuid4(),
                    status="active", created_at=timezone.now(), updated_at=timezone.now(),
                )
            else:
                messages.warning(request, "Completa primero tu ficha médica para continuar.")
                return redirect("patient_medical_record", step=1)
        request.user_profile = profile
        request.patient = patient
        request.account_profile = profile
        return view(request, *args, **kwargs)

    return wrapped
