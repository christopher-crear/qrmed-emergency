import mimetypes
import logging
import uuid
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import connection


logger = logging.getLogger(__name__)


class SupabaseError(Exception):
    pass


def _auth_headers(token=None, service=False):
    key = settings.SUPABASE_SERVICE_ROLE_KEY if service else settings.SUPABASE_ANON_KEY
    headers = {"apikey": key, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key and not key.startswith(("sb_secret_", "sb_publishable_")):
        # Las claves JWT legacy aceptan Bearer. Las nuevas claves sb_* deben
        # enviarse únicamente mediante apikey.
        headers["Authorization"] = f"Bearer {key}"
    return headers


def normalize_storage_path(path, bucket):
    """Acepta rutas simples o prefijadas con el bucket y devuelve el objeto real."""
    if not path:
        return ""
    value = str(path).strip().replace("\\", "/")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        storage_markers = (
            f"/storage/v1/object/public/{bucket}/",
            f"/storage/v1/object/sign/{bucket}/",
            f"/storage/v1/object/{bucket}/",
        )
        for marker in storage_markers:
            if marker in parsed.path:
                return unquote(parsed.path.split(marker, 1)[1])
        return value
    if value.startswith("/media/"):
        return value
    value = value.lstrip("/")
    prefixes = (
        f"{bucket}/",
        f"public/{bucket}/",
        f"object/public/{bucket}/",
        f"storage/v1/object/public/{bucket}/",
        f"object/sign/{bucket}/",
        f"storage/v1/object/sign/{bucket}/",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def sign_in(email, password):
    if settings.DEMO_MODE:
        if email.lower() == settings.DEMO_ADMIN_EMAIL.lower() and password == settings.DEMO_ADMIN_PASSWORD:
            return {"access_token": "demo-token", "refresh_token": "demo-refresh", "user": {"id": "11111111-1111-1111-1111-111111111111", "email": email}}
        raise SupabaseError("Correo o contraseña incorrectos.")
    try:
        response = requests.post(
            f"{settings.SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers=_auth_headers(), json={"email": email, "password": password}, timeout=15,
        )
    except requests.RequestException as exc:
        raise SupabaseError("No se pudo conectar con Supabase Auth.") from exc
    if not response.ok:
        detail = response.json().get("msg") if response.headers.get("content-type", "").startswith("application/json") else None
        raise SupabaseError(detail or "Correo o contraseña incorrectos.")
    return response.json()


def get_auth_user(access_token):
    """Obtiene del servidor de Supabase la identidad asociada al token OAuth."""
    if settings.DEMO_MODE:
        raise SupabaseError("El acceso social no está disponible en modo demostración.")
    try:
        response = requests.get(
            f"{settings.SUPABASE_URL}/auth/v1/user",
            headers=_auth_headers(access_token),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise SupabaseError("No se pudo validar el acceso social con Supabase.") from exc
    if not response.ok:
        raise SupabaseError("La sesión social no es válida o expiró.")
    return response.json()


def update_password(access_token, new_password):
    if settings.DEMO_MODE:
        return True
    response = requests.put(
        f"{settings.SUPABASE_URL}/auth/v1/user", headers=_auth_headers(access_token),
        json={"password": new_password}, timeout=15,
    )
    if not response.ok:
        raise SupabaseError(response.json().get("msg", "No se pudo actualizar la contraseña."))
    return True


def public_storage_url(path, bucket):
    if not path:
        return ""
    value = normalize_storage_path(path, bucket)
    if value.startswith(("http://", "https://", "/media/")):
        return value
    if settings.DEMO_MODE:
        return settings.MEDIA_URL + value.lstrip("/")
    return f"{settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{value}"


def _storage_auth_options(access_token=None):
    """Credenciales válidas para Storage, sin exponerlas en logs ni respuestas."""
    options = []
    if access_token:
        options.append(("sesión del administrador", _auth_headers(access_token)))
    if settings.SUPABASE_SERVICE_ROLE_KEY:
        options.append(("clave de servicio", _auth_headers(service=True)))
    return options


def storage_signed_url(path, bucket, expires_in=3600, access_token=None):
    """Genera una URL temporal cuando el bucket es privado; admite buckets públicos."""
    if not path:
        return ""
    value = normalize_storage_path(path, bucket)
    if value.startswith(("http://", "https://", "/media/")) or settings.DEMO_MODE:
        return public_storage_url(value, bucket)
    if not access_token and not settings.SUPABASE_SERVICE_ROLE_KEY:
        return public_storage_url(value, bucket)
    object_path = value.lstrip("/")
    cache_key = "storage-signed:" + sha256(f"{bucket}:{object_path}".encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        return cached
    response = None
    auth_source = ""
    for auth_source, headers in _storage_auth_options(access_token):
        try:
            attempt = requests.post(
                f"{settings.SUPABASE_URL}/storage/v1/object/sign/{bucket}/{object_path}",
                headers=headers,
                json={"expiresIn": expires_in},
                timeout=15,
            )
        except requests.RequestException as exc:
            logger.warning(
                "No fue posible conectar con Supabase Storage usando %s: %s",
                auth_source, exc.__class__.__name__,
            )
            continue
        if attempt.ok:
            response = attempt
            break
        logger.warning(
            "Supabase rechazó firma usando %s bucket=%s path=%s status=%s respuesta=%s",
            auth_source, bucket, object_path, attempt.status_code,
            (attempt.text or "")[:180],
        )
    if response is None:
        return public_storage_url(value, bucket)
    signed = response.json().get("signedURL") or response.json().get("signedUrl") or ""
    if signed.startswith(("http://", "https://")):
        result = signed
    elif signed.startswith("/storage/"):
        result = f"{settings.SUPABASE_URL}{signed}"
    elif signed.startswith("/object/"):
        result = f"{settings.SUPABASE_URL}/storage/v1{signed}"
    else:
        return public_storage_url(value, bucket)
    cache.set(cache_key, result, timeout=max(60, expires_in - 60))
    return result


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def _matches_keywords(path, keywords):
    if not keywords:
        return True
    filename = Path(str(path)).name.lower()
    return any(str(keyword).lower() in filename for keyword in keywords)


def _storage_matches_from_database(bucket, identifiers, keywords=()):
    """Busca objetos ligados al UUID incluso cuando la ruta no se guardó en el perfil."""
    terms = [str(value).strip().lower() for value in identifiers if value]
    if not terms:
        return []
    clauses = " OR ".join(["LOWER(name) LIKE %s"] * len(terms))
    params = [bucket, *[f"%{term}%" for term in terms]]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT name
                  FROM storage.objects
                 WHERE bucket_id = %s
                   AND ({clauses})
                 ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                 LIMIT 25
                """,
                params,
            )
            return [
                row[0] for row in cursor.fetchall()
                if row and row[0] and _matches_keywords(row[0], keywords)
            ]
    except Exception:
        # En SQLite/demo no existe el esquema storage. La búsqueda REST de abajo
        # sigue funcionando contra Supabase.
        return []


def _storage_matches_from_api(bucket, identifiers, keywords=()):
    if settings.DEMO_MODE or not settings.SUPABASE_SERVICE_ROLE_KEY:
        return []
    found = []
    roots = ("", "avatars", "profiles", "profile", "users", "photos")
    for identifier in [str(value).strip() for value in identifiers if value]:
        queries = [(root, identifier) for root in roots]
        queries.extend(
            (prefix, "")
            for prefix in (
                identifier,
                f"avatars/{identifier}",
                f"profiles/{identifier}",
                f"profile/{identifier}",
                f"users/{identifier}",
                f"photos/{identifier}",
            )
        )
        for prefix, search in queries:
            payload = {
                "prefix": prefix,
                "limit": 100,
                "offset": 0,
                "sortBy": {"column": "updated_at", "order": "desc"},
            }
            if search:
                payload["search"] = search
            try:
                response = requests.post(
                    f"{settings.SUPABASE_URL}/storage/v1/object/list/{bucket}",
                    headers=_auth_headers(service=True),
                    json=payload,
                    timeout=12,
                )
            except requests.RequestException:
                continue
            if not response.ok:
                continue
            for item in response.json() or []:
                name = item.get("name") if isinstance(item, dict) else ""
                if not name:
                    continue
                full_path = f"{prefix.rstrip('/')}/{name}".lstrip("/") if prefix else name
                if full_path.lower().endswith(IMAGE_EXTENSIONS) and _matches_keywords(full_path, keywords):
                    found.append(full_path)
            if found:
                break
        if found:
            break
    return found


def storage_image_candidates(bucket, stored_path="", identifiers=(), keywords=()):
    """Devuelve rutas plausibles, priorizando la ruta guardada y luego el UUID."""
    cache_key = "storage-candidates:" + sha256(
        f"{bucket}:{stored_path}:{','.join(str(x) for x in identifiers if x)}:{','.join(keywords)}".encode()
    ).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    candidates = []
    normalized = normalize_storage_path(stored_path, bucket)
    if normalized:
        candidates.append(normalized)
    else:
        candidates.extend(_storage_matches_from_database(bucket, identifiers, keywords))
        candidates.extend(_storage_matches_from_api(bucket, identifiers, keywords))

    unique = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    cache.set(cache_key, unique, timeout=900)
    return unique


def storage_image_signed_url(
    bucket, stored_path="", identifiers=(), keywords=(), expires_in=900,
    access_token=None,
):
    """Devuelve una URL temporal para el primer objeto de imagen válido.

    El navegador descarga directamente desde Supabase. Esto evita que una
    restricción local de `requests` o un encabezado intermedio convierta una
    imagen privada válida en un 404 del proxy de Django.
    """
    candidate_groups = [storage_image_candidates(bucket, stored_path, identifiers, keywords)]
    if normalize_storage_path(stored_path, bucket) and any(identifiers):
        candidate_groups.append(storage_image_candidates(bucket, "", identifiers, keywords))
    for candidates in candidate_groups:
        for candidate in candidates:
            if candidate.startswith(("http://", "https://", "/media/")):
                return candidate
            signed = storage_signed_url(
                candidate, bucket, expires_in=expires_in, access_token=access_token,
            )
            if "/storage/v1/object/sign/" in signed:
                return signed
    return ""


def storage_image_bytes(bucket, stored_path="", identifiers=(), keywords=(), access_token=None):
    """Descarga una imagen privada con credenciales del servidor.

    El navegador recibe después la imagen desde Django, por lo que no depende de
    que el bucket sea público ni de URLs firmadas visibles en el HTML.
    """
    candidate_groups = [storage_image_candidates(bucket, stored_path, identifiers, keywords)]
    if normalize_storage_path(stored_path, bucket) and any(identifiers):
        # Si la ruta guardada quedó obsoleta, se intenta después la recuperación
        # automática por UUID sin retrasar el caso normal.
        candidate_groups.append(None)
    for candidates in candidate_groups:
        if candidates is None:
            candidates = storage_image_candidates(bucket, "", identifiers, keywords)
        for candidate in candidates:
            cache_key = "storage-image:" + sha256(f"{bucket}:{candidate}".encode()).hexdigest()
            cached = cache.get(cache_key)
            if cached:
                return cached

            if candidate.startswith(("http://", "https://")):
                endpoints = [(candidate, {})]
            elif settings.DEMO_MODE:
                try:
                    with default_storage.open(candidate, "rb") as source:
                        content = source.read()
                    result = (content, mimetypes.guess_type(candidate)[0] or "image/jpeg")
                    cache.set(cache_key, result, timeout=1800)
                    return result
                except (FileNotFoundError, OSError):
                    continue
            else:
                object_path = candidate.lstrip("/")
                endpoints = []
                for _, auth_headers in _storage_auth_options(access_token):
                    endpoints.extend([
                        (
                            f"{settings.SUPABASE_URL}/storage/v1/object/authenticated/{bucket}/{object_path}",
                            auth_headers,
                        ),
                        (
                            f"{settings.SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}",
                            auth_headers,
                        ),
                    ])
                signed_url = storage_signed_url(
                    object_path, bucket, expires_in=300, access_token=access_token,
                )
                if "/object/sign/" in signed_url:
                    # Una URL firmada ya contiene su autorización en el token.
                    # No se debe reenviar el encabezado Bearer del servicio.
                    endpoints.append((signed_url, {}))

            for endpoint, endpoint_headers in endpoints:
                try:
                    response = requests.get(endpoint, headers=endpoint_headers, timeout=18)
                except requests.RequestException:
                    continue
                if not response.ok:
                    continue
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    content_type = mimetypes.guess_type(candidate)[0] or "image/jpeg"
                result = (response.content, content_type)
                cache.set(cache_key, result, timeout=1800)
                return result
    logger.warning(
        "No se pudo recuperar imagen de Supabase bucket=%s stored_path=%s identifiers=%s",
        bucket, normalize_storage_path(stored_path, bucket),
        [str(value) for value in identifiers if value],
    )
    return None


def upload_file(uploaded_file, bucket, folder, filename_prefix="", access_token=None):
    extension = Path(uploaded_file.name).suffix.lower()
    object_path = f"{folder}/{filename_prefix}{uuid.uuid4().hex}{extension}"
    if settings.DEMO_MODE:
        saved = default_storage.save(object_path, uploaded_file)
        return saved
    if not access_token and not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise SupabaseError("Falta una sesión válida o SUPABASE_SERVICE_ROLE_KEY para subir archivos.")
    content_type = uploaded_file.content_type or mimetypes.guess_type(uploaded_file.name)[0] or "application/octet-stream"
    # ImageField/Pillow may inspect the stream before this helper is called.
    # Always rewind it so Supabase receives the complete file instead of an
    # empty or truncated payload.
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        pass
    payload = uploaded_file.read()
    if not payload:
        raise SupabaseError("El archivo seleccionado está vacío o no pudo leerse.")
    last_status = None
    for auth_source, base_headers in _storage_auth_options(access_token):
        headers = dict(base_headers)
        headers.update({"Content-Type": content_type, "x-upsert": "true"})
        try:
            response = requests.post(
                f"{settings.SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}",
                headers=headers, data=payload, timeout=30,
            )
        except requests.RequestException as exc:
            logger.warning(
                "No fue posible subir a Supabase Storage usando %s: %s",
                auth_source, exc.__class__.__name__,
            )
            continue
        if response.ok:
            return object_path
        last_status = response.status_code
        logger.warning(
            "Supabase rechazó subida usando %s bucket=%s path=%s status=%s respuesta=%s",
            auth_source, bucket, object_path, response.status_code,
            (response.text or "")[:180],
        )
    suffix = f" (HTTP {last_status})" if last_status else ""
    raise SupabaseError(f"No se pudo subir el archivo a Supabase Storage{suffix}.")


def versioned_media_url(url, updated_at=None):
    """Append a stable cache key that changes whenever the database row changes."""
    if not url or not updated_at:
        return url or ""
    try:
        version = int(updated_at.timestamp() * 1_000_000)
    except (AttributeError, OSError, OverflowError, ValueError):
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version}"
