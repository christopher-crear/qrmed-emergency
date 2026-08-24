import re


def medical_profile_missing_fields(patient):
    """Campos mínimos necesarios antes de publicar un QR de emergencia."""
    required = (
        ("first_name", "nombres"),
        ("last_name", "apellidos"),
        ("id_number", "cédula"),
        ("birth_date", "fecha de nacimiento"),
        ("sex", "sexo"),
        ("phone", "teléfono"),
        ("email", "correo electrónico"),
        ("address", "dirección"),
        ("city", "ciudad"),
        ("blood_type", "tipo de sangre"),
        ("emergency_name", "nombre del contacto de emergencia"),
        ("emergency_relationship", "parentesco del contacto"),
        ("emergency_phone", "teléfono de emergencia"),
    )
    missing = [label for name, label in required if not str(getattr(patient, name, "") or "").strip()]
    cedula = re.sub(r"\D", "", str(getattr(patient, "id_number", "") or ""))
    if "cédula" not in missing and len(cedula) != 10:
        missing.append("cédula válida")
    return missing


def medical_profile_is_complete(patient):
    return not medical_profile_missing_fields(patient)
