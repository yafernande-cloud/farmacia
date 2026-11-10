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

La comparación es conservadora: no usa similitud difusa ni prefijos, porque una coincidencia
parcial de texto no demuestra que sea el mismo producto.
"""

from __future__ import annotations

from app.domain.products import normalize_name

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


def _tokens(value: str) -> list[str]:
    return [token for token in normalize_name(value).split() if token]


def _meaningful(tokens: list[str]) -> list[str]:
    filtered = [token for token in tokens if token not in _STOPWORDS]
    return filtered or tokens


def classify_match(query: str, name: str | None) -> tuple[str, list[str]]:
    """Devuelve `(relation, reasons)`. Las causas explican la clasificación y no la maquillan."""
    query_tokens = _meaningful(_tokens(query or ""))
    if not query_tokens:
        return "unverified", ["empty_query"]
    name_tokens = _tokens(name or "")
    if not name_tokens:
        return "unverified", ["name_missing"]

    if all(token in name_tokens for token in query_tokens):
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
