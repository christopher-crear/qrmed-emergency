import base64
import json
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from panel.models import Profile
from panel.services import storage_signed_url


class Command(BaseCommand):
    help = "Comprueba PostgreSQL, configuración y acceso privado a Supabase Storage sin mostrar claves."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Diagnóstico QRMed / Supabase Storage"))
        self._line("SUPABASE_URL", settings.SUPABASE_URL or "NO CONFIGURADA")
        self._line("Bucket de perfiles", settings.SUPABASE_PROFILE_BUCKET)
        self._line("Bucket de pacientes", settings.SUPABASE_PATIENT_BUCKET)
        self._line("Bucket de pagos", settings.SUPABASE_PAYMENT_BUCKET)
        self._line("Bucket de bancos", settings.SUPABASE_BANK_BUCKET)

        key = settings.SUPABASE_SERVICE_ROLE_KEY or ""
        self._line("Clave de servicio", "configurada" if key else "NO CONFIGURADA")
        if key:
            self._line("Tipo de clave", "secret nueva" if key.startswith("sb_secret_") else "JWT legacy")
            self._describe_legacy_jwt(key)

        try:
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute("SELECT current_database()")
                    database_name = cursor.fetchone()[0]
                else:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                    database_name = connection.settings_dict.get("NAME", "base local")
            self.stdout.write(self.style.SUCCESS(f"[OK] PostgreSQL conectado: {database_name}"))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"[ERROR] PostgreSQL: {exc.__class__.__name__}: {exc}"))
            return

        profile = (
            Profile.objects.exclude(avatar_path__isnull=True)
            .exclude(avatar_path="")
            .order_by("-updated_at")
            .first()
        )
        if not profile:
            self.stdout.write(self.style.WARNING("[AVISO] No existe un avatar_path para probar."))
            return

        self._line("Perfil de prueba", str(profile.id))
        self._line("Ruta de prueba", profile.avatar_path)
        signed = storage_signed_url(
            profile.avatar_path,
            settings.SUPABASE_PROFILE_BUCKET,
            expires_in=120,
        )
        if "/storage/v1/object/sign/" not in signed:
            self.stdout.write(self.style.ERROR(
                "[ERROR] Storage no generó URL firmada. Revisa los mensajes inmediatamente anteriores."
            ))
            return
        self.stdout.write(self.style.SUCCESS("[OK] URL privada firmada correctamente."))
        try:
            response = requests.get(signed, timeout=20)
            content_type = response.headers.get("content-type", "sin content-type")
            self._line("Descarga firmada", f"HTTP {response.status_code} · {content_type} · {len(response.content)} bytes")
            if response.ok and content_type.startswith("image/"):
                self.stdout.write(self.style.SUCCESS("[OK] La imagen privada se puede descargar."))
            else:
                self.stdout.write(self.style.ERROR("[ERROR] La URL se firmó, pero el archivo no se descargó como imagen."))
        except requests.RequestException as exc:
            self.stdout.write(self.style.ERROR(f"[ERROR] Red hacia Storage: {exc.__class__.__name__}: {exc}"))

    def _line(self, label, value):
        self.stdout.write(f"- {label}: {value}")

    def _describe_legacy_jwt(self, key):
        try:
            payload_part = key.split(".")[1]
            payload_part += "=" * (-len(payload_part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_part).decode())
            self._line("Rol declarado por la clave", payload.get("role", "sin role"))
            self._line("Proyecto declarado", payload.get("ref", "sin ref"))
            if payload.get("exp"):
                expires = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
                self._line("Expiración", expires.isoformat())
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            self.stdout.write(self.style.WARNING("[AVISO] La clave configurada no se pudo interpretar como JWT."))
