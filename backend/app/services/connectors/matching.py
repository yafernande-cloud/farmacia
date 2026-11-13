"""Clasificación textual de coincidencias de una búsqueda de proveedor.

Responde a una sola pregunta: **¿el nombre publicado confirma la consulta?** No decide equivalencia
farmacéutica, no compara principios activos, dosis ni formas, y no elimina resultados. Un producto
que el proveedor devolvió pero cuyo nombre no confirma la consulta queda `related`: se muestra
aparte, nunca se presenta como coincidencia ni se descarta en silencio.

Relaciones:

* `direct`      — el nombre contiene todos los términos significativos de la consulta, o la
                  consulta completa como frase.
* `related`     — el proveedor lo devolvió, pero el nombre solo confirma parte de la consulta o
                  no la confirma.
* `unverified`  — falta el nombre o la consulta para poder comparar.

La comparación es conservadora: **no** usa similitud difusa, prefijos ni distancia de edición. Solo
reconoce variantes de escritura que no cambian la identidad: singular/plural, abreviaturas
farmacéuticas del diccionario (`FORMS`), envases (`PACKAGING`) y formas que se escriben juntas
(«x30», «caja30», «500mg»). La concentración y la forma farmacéutica siguen siendo obligatorias: una
dosis o una forma distinta deja el resultado como `related`.
"""

from __future__ import annotations

import re

from app.domain.products import FORMS, PACKAGING, normalize_name
from app.domain.units import UNITS

MATCH_RELATIONS = ("direct", "related", "unverified")

# Conectores comunes que no identifican un producto por sí solos.
_STOPWORDS = {
    "de",
    "del",
    "con",
    "sin",
    "y",
    "el",
    "la",
    "los",
    "las",
    "x",
    "por",
    "para",
    "un",
    "una",
}

# Los envases no son una forma farmacéutica: si el nombre no declara ninguno, no invalidan.
_PACKAGING_TOKENS = frozenset(PACKAGING.values())

# Términos que no identifican un producto para la consulta de respaldo del proveedor.
_DROPPED = _STOPWORDS | set(FORMS) | set(PACKAGING) | set(UNITS)


def _raw_tokens(value: str) -> list[str]:
    """Separa compuestos que suelen escribirse juntos: «x30», «caja30», «500mg»."""
    tokens: list[str] = []
    for word in normalize_name(value).split():
        tokens.extend(re.findall(r"[a-z]+|\d+", word))
    return tokens


def _canonical(token: str) -> str:
    """Forma comparable: diccionario farmacéutico y plural conservador."""
    if token in FORMS:
        return FORMS[token]
    if token in PACKAGING:
        return PACKAGING[token]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _tokens(value: str) -> list[str]:
    return [_canonical(token) for token in _raw_tokens(value)]


def _meaningful(tokens: list[str]) -> list[str]:
    filtered = [token for token in tokens if token not in _STOPWORDS]
    return filtered or tokens


def _confirmed(query_tokens: list[str], name_tokens: list[str]) -> bool:
    """¿Están todos los términos significativos? Solo se admite omitir un envase."""
    name_set = set(name_tokens)
    missing = [token for token in query_tokens if token not in name_set]
    if not missing:
        return True
    # «caja», «frasco» o «blister» describen el empaque, no la forma farmacéutica. Si el nombre
    # publicado no declara ningún envase, no se exige; si declara otro, tampoco confirma.
    if not any(token in _PACKAGING_TOKENS for token in name_tokens):
        return all(token in _PACKAGING_TOKENS for token in missing)
    return False


def reduce_query(query: str) -> str | None:
    """Consulta de respaldo: solo los términos que identifican el producto.

    Descarta conectores, formas, envases, unidades y cifras. Nunca inventa ni completa términos: si
    no queda nada distinto de la consulta original, devuelve `None` y no hay reintento. El texto
    reducido solo sirve para que el proveedor devuelva candidatos; la clasificación se hace siempre
    contra la consulta original.
    """
    kept = [
        token
        for token in _raw_tokens(query)
        if not token.isdigit() and token not in _DROPPED
    ]
    reduced = " ".join(kept).strip()
    original = " ".join((query or "").split())
    if not reduced or reduced.casefold() == original.casefold():
        return None
    return reduced


def classify_match(query: str, name: str | None) -> tuple[str, list[str]]:
    """Devuelve `(relation, reasons)`. Las causas explican la clasificación y no la maquillan."""
    query_tokens = _meaningful(_tokens(query or ""))
    if not query_tokens:
        return "unverified", ["empty_query"]
    name_tokens = _tokens(name or "")
    if not name_tokens:
        return "unverified", ["name_missing"]

    if _confirmed(query_tokens, name_tokens):
        return "direct", ["all_query_terms_in_name"]

    # Una marca de varias palabras puede publicarse sin espacios o con guiones.
    phrase = "".join(query_tokens)
    if phrase and phrase in "".join(name_tokens):
        return "direct", ["query_phrase_in_name"]

    if any(token in name_tokens for token in query_tokens):
        # El nombre confirma solo una parte: puede ser otra presentación o una concentración
        # distinta. No se afirma coincidencia ni se retira el resultado.
        return "related", ["partial_query_match"]
    return "related", ["query_not_confirmed_by_name"]
