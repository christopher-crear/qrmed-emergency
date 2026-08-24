import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_int(name, default=0):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-key")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x.strip()]
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "panel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            "panel.context_processors.admin_context",
        ],
    },
}]
WSGI_APPLICATION = "config.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL:
    # Supabase ya administra el pool de conexiones con Supavisor. Mantener una
    # conexión persistente adicional por cada proceso/hilo de Django puede
    # agotar rápidamente el límite del pool (especialmente en el puerto 5432,
    # modo sesión). Por eso el valor seguro por defecto es 0: Django cierra la
    # conexión al terminar cada solicitud. Se puede sobrescribir de forma
    # explícita con DATABASE_CONN_MAX_AGE si el plan de Supabase lo permite.
    DATABASE_CONN_MAX_AGE = max(0, env_int("DATABASE_CONN_MAX_AGE", 0))
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=DATABASE_CONN_MAX_AGE,
            conn_health_checks=DATABASE_CONN_MAX_AGE > 0,
            ssl_require=True,
        )
    }
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].setdefault("connect_timeout", 10)
    # Supabase transaction pooler (6543) does not support prepared statements.
    if ":6543/" in DATABASE_URL:
        DATABASES["default"]["OPTIONS"]["prepare_threshold"] = None
        # Los cursores de servidor dependen de conservar la misma conexión y
        # no son compatibles con un pooler en modo transacción.
        DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es-ec"
TIME_ZONE = "America/Guayaquil"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Caché local pequeño y acotado: evita recalcular QR, volver a firmar URLs de
# Storage y releer la sesión desde PostgreSQL en cada recurso de una página.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "qrmed-runtime-cache",
        "TIMEOUT": 300,
        "OPTIONS": {"MAX_ENTRIES": 1000, "CULL_FREQUENCY": 3},
    }
}
# Render ejecuta varios workers y LocMemCache no se comparte entre procesos.
# `cached_db` podía entregar copias diferentes de un mismo carrito, haciendo que
# desapareciera al aplicar/quitar un cupón y reapareciera en la siguiente visita.
# La sesión de base de datos mantiene un único estado autoritativo por usuario.
SESSION_ENGINE = "django.contrib.sessions.backends.db"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_PATIENT_BUCKET = os.getenv("SUPABASE_PATIENT_BUCKET", "patient-photos")
SUPABASE_PAYMENT_BUCKET = os.getenv("SUPABASE_PAYMENT_BUCKET", "payment-proofs")
QRMED_COMPANY_NAME = os.getenv("QRMED_COMPANY_NAME", "QRMed Emergency")
QRMED_COMPANY_TAX_ID = os.getenv("QRMED_COMPANY_TAX_ID", "")
QRMED_COMPANY_ADDRESS = os.getenv("QRMED_COMPANY_ADDRESS", "Loja, Ecuador")
QRMED_COMPANY_PHONE = os.getenv("QRMED_COMPANY_PHONE", "")
QRMED_COMPANY_EMAIL = os.getenv("QRMED_COMPANY_EMAIL", "qrmedicsupport@gmail.com")
SUPABASE_PROFILE_BUCKET = os.getenv("SUPABASE_PROFILE_BUCKET", "profile-images")
SUPABASE_BANK_BUCKET = os.getenv("SUPABASE_BANK_BUCKET", "bank-assets")
# Compatibilidad con el .env de las entregas anteriores. En este proyecto los
# buckets reales, verificados en Supabase, son patient-photos y profile-images.
if SUPABASE_PATIENT_BUCKET == "patient-files":
    SUPABASE_PATIENT_BUCKET = "patient-photos"
if SUPABASE_PROFILE_BUCKET == "profiles":
    SUPABASE_PROFILE_BUCKET = "profile-images"
# Una conexión PostgreSQL configurada siempre tiene prioridad sobre cualquier
# valor heredado de DEMO_MODE en Render.
DEMO_MODE = False if DATABASE_URL else env_bool("DEMO_MODE", False)
DEMO_ADMIN_EMAIL = os.getenv("DEMO_ADMIN_EMAIL", "admin@qrmed.ec")
DEMO_ADMIN_PASSWORD = os.getenv("DEMO_ADMIN_PASSWORD", "admin123")

LOGIN_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "3600"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    X_FRAME_OPTIONS = "DENY"
