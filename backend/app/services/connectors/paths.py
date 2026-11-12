"""Lectura declarativa de campos: rutas JSON y transformaciones acotadas.

El perfil no ejecuta código. Solo puede pedir una ruta (`batchs[].stock`), una constante o una
plantilla, y una de tres transformaciones (`first`, `sum`, `join`). Eso mantiene el runtime
auditable y evita scripts generados por IA.
"""

from __future__ import annotations

import re
import string
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

_TOKEN = re.compile(r"^([A-Za-z0-9_\-]+)(?:\[(\d*)\])?$")
_MISSING = object()


def _tokens(path: str) -> list[tuple[str, bool, int | None]]:
    path = (path or "").strip()
    if path.startswith("$."):
        path = path[2:]
    elif path == "$":
        return []
    if not path:
        return []
    out: list[tuple[str, bool, int | None]] = []
    for part in path.split("."):
        match = _TOKEN.match(part)
        if match is None:
            raise ValueError(f"Ruta no válida: {path!r}")
        name, bracket = match.group(1), match.group(2)
        if bracket is None:
            out.append((name, False, None))
        elif bracket == "":
            out.append((name, True, None))
        else:
            out.append((name, False, int(bracket)))
    return out


def resolve_all(root: Any, path: str) -> list[Any]:
    """Devuelve todos los valores que resuelven la ruta. Una ruta inválida devuelve []."""
    try:
        tokens = _tokens(path)
    except ValueError:
        return []
    current = [root]
    for name, is_array, index in tokens:
        nxt: list[Any] = []
        for value in current:
            if not isinstance(value, dict) or name not in value:
                continue
            child = value[name]
            if is_array:
                if isinstance(child, list):
                    nxt.extend(child)
            elif index is not None:
                if isinstance(child, list) and 0 <= index < len(child):
                    nxt.append(child[index])
            else:
                nxt.append(child)
        current = nxt
    return current


def resolve_one(root: Any, path: str) -> Any:
    for value in resolve_all(root, path):
        if value is not None:
            return value
    return None


def replace_at(root: Any, path: str, replacement: Any) -> Any:
    """Copia de `root` con el valor resuelto por `path` reemplazado.

    No muta la entrada. Se usa para describir la estructura de un sobre sin mirar el contenido de
    los resultados: el resto de las claves se conserva tal como llegó.
    """
    try:
        tokens = _tokens(path)
    except ValueError:
        return root
    if not tokens:
        return replacement

    def walk(node: Any, position: int) -> Any:
        name, is_array, index = tokens[position]
        if not isinstance(node, dict) or name not in node:
            return node
        copied = dict(node)
        child = node[name]
        last = position == len(tokens) - 1
        if last and is_array:
            if isinstance(child, list):
                copied[name] = [replacement for _ in child]
        elif last and index is not None:
            if isinstance(child, list) and 0 <= index < len(child):
                values = list(child)
                values[index] = replacement
                copied[name] = values
        elif last:
            copied[name] = replacement
        elif is_array and isinstance(child, list):
            copied[name] = [walk(item, position + 1) for item in child]
        elif index is not None and isinstance(child, list) and 0 <= index < len(child):
            values = list(child)
            values[index] = walk(values[index], position + 1)
            copied[name] = values
        else:
            copied[name] = walk(child, position + 1)
        return copied

    return walk(root, 0)


def apply_transform(values: list[Any], transform: str) -> Any:
    if transform == "join":
        texts = [v for v in values if isinstance(v, str) and v.strip()]
        return ", ".join(t.strip() for t in texts) if texts else None
    if transform == "sum":
        total = Decimal("0")
        seen = False
        for value in values:
            amount = as_decimal(value)
            if amount is None:
                return None
            total += amount
            seen = True
        return total if seen else None
    # `first` es el comportamiento por defecto.
    for value in values:
        if value is not None:
            return value
    return None


def as_text(value: Any, *, allow_number: bool = False) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
        return text or None
    if allow_number and isinstance(value, (int, float)):
        return str(value)
    return None


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float, str)):
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def as_url(value: Any) -> str | None:
    text = as_text(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "1", "yes", "si", "sí"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    return None


def render_template(template: str, item: Any) -> str | None:
    """Rellena una plantilla con campos del ítem. Un campo ausente invalida la plantilla."""
    if not isinstance(item, dict):
        return None
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    except ValueError:
        return None
    values: dict[str, Any] = {}
    for field in fields:
        if not field or field.isdigit():
            return None
        value = item.get(field)
        if value is None:
            return None
        values[field] = value
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError, IndexError):
        return None
    return rendered.strip() or None
