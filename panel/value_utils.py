"""Normaliza valores heredados antes de mostrarlos en la interfaz.

Algunas instalaciones antiguas guardaron campos de texto/lista serializados como
JSON, representaciones de Python o arreglos PostgreSQL (por ejemplo
``{"valor": "IESS"}``, ``['Penicilina']`` o ``{Penicilina,Latex}``).  La UI no
debe exponer esas llaves/corchetes: este módulo convierte esos formatos a texto
humano sin modificar la base de datos hasta que el usuario guarde el formulario.
"""
from __future__ import annotations

import ast
import csv
import io
import json
from collections.abc import Mapping


PREFERRED_KEYS = (
    "value", "valor", "label", "etiqueta", "name", "nombre", "text", "texto",
    "description", "descripcion", "descripción",
)


def _unique(parts):
    seen = set()
    output = []
    for part in parts:
        item = str(part or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _split_postgres_array(value: str):
    inner = value[1:-1].strip()
    if not inner:
        return []
    try:
        reader = csv.reader(io.StringIO(inner), skipinitialspace=True)
        return next(reader)
    except Exception:
        return [part.strip() for part in inner.split(",")]


def _parse_structured_string(value: str):
    text = value.strip()
    if not text:
        return None

    if (text.startswith("[") and text.endswith("]")) or (
        text.startswith("{") and text.endswith("}")
    ) or (text.startswith("(") and text.endswith(")")):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except (ValueError, TypeError, SyntaxError, json.JSONDecodeError):
                continue
            if parsed != text:
                return parsed

    # PostgreSQL text[] puede llegar como {uno,dos} cuando el driver no lo
    # convierte automáticamente a lista.
    if text.startswith("{") and text.endswith("}"):
        return _split_postgres_array(text)
    return None


def humanize_value(value, default="", separator=", "):
    """Devuelve un texto de presentación sin sintaxis de JSON/Python/arrays."""
    if value is None:
        return default

    if isinstance(value, Mapping):
        lowered = {str(key).strip().casefold(): key for key in value.keys()}
        for preferred in PREFERRED_KEYS:
            original_key = lowered.get(preferred.casefold())
            if original_key is not None:
                preferred_value = humanize_value(value.get(original_key), "", separator)
                if preferred_value:
                    return preferred_value
        parts = _unique(humanize_value(item, "", separator) for item in value.values())
        return separator.join(parts) if parts else default

    if isinstance(value, (list, tuple, set)):
        parts = _unique(humanize_value(item, "", separator) for item in value)
        return separator.join(parts) if parts else default

    if isinstance(value, bool):
        return "Sí" if value else "No"

    text = str(value).strip()
    if not text or text in {"[]", "{}", "()", "null", "None"}:
        return default

    parsed = _parse_structured_string(text)
    if parsed is not None:
        return humanize_value(parsed, default, separator)

    # Solo retiramos comillas cuando envuelven el valor completo. No tocamos
    # llaves que formen parte de un texto normal como "Casa {A}".
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text or default
