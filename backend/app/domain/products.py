"""Modelo de producto de un faltante y su interpretación determinista (HLD §7, §9).

Se separan cinco cosas que antes se mezclaban:
  * potencia/concentración (`strengths`): 400 mg · 100 mg/5 mL · 2,5 %
  * contenido del envase (`content`): x 20 · 120 mL
  * presentación comercial (`form` + `content` + `packaging`)
  * cantidad solicitada para comprar (`requested_*`): solo sugerencia, nunca parte de la identidad
  * medidas sin rol o sin unidad (`unassigned`): "400" a secas queda PENDIENTE, jamás se asume mg

Cada atributo declara su procedencia (`Basis`): EXPLICIT (lo escribió la persona), INFERRED (lo
dedujo una regla por contexto), CONFIRMED (lo validó un usuario autorizado). Una inferencia nunca
pasa a confirmada sola. Lo que no se sabe no existe (None / pendiente): no se completa por suposición.
"""

import re
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum

from app.core.errors import DomainError
from app.domain.numberwords import convert_number_words
from app.domain.units import (
    NUM,
    UNIT_ALT,
    UNITS,
    Dimension,
    Measure,
    build_measure,
    canonical_unit,
    fmt,
    parse_number,
)

POLICY_VERSION = "h1a-conservative-2"
SCHEMA_VERSION = 2


class Basis(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"


SOLID = {Basis.EXPLICIT, Basis.CONFIRMED}
COMMERCIAL_FIELDS = ("brand", "laboratory", "manufacturer", "registration_holder", "marketer", "gtin",
                     "sanitary_registration")  # fmt: skip
PHARMA_ATTRS = {
    "strength",
    "form",
    "release",
    "measure",
    "active_ingredient",
}  # el resto es identidad comercial

STOPWORDS = {
    "falta", "faltan", "falto", "faltaron", "no", "hay", "queda", "quedan", "necesito",
    "necesitamos", "se", "acabo", "acabaron", "agoto", "agotado", "urgente", "de", "del", "la",
    "el", "los", "las", "un", "una", "unos", "unas", "por", "favor", "porfa", "pedir", "comprar",
    "reponer", "ya", "mas", "tampoco", "y", "para", "con", "en", "tenemos", "tengo", "stock", "x",
    "nos", "lo", "le", "les", "me", "hoy", "nada", "sin", "casi", "poco", "poca", "pocos", "ahi",
    "aca", "esta", "estan", "estamos", "quedamos", "acaba", "acabando", "faltando", "pedido",
    # Verbos con los que el auxiliar introduce el reporte: nunca son el nombre del producto.
    "anota", "anotar", "anotame", "anotamos", "agrega", "agregar", "agregame", "agregamos",
    "suma", "sumar", "sumale", "sumamos", "apunta", "apuntar", "apuntame", "apuntamos",
}  # fmt: skip
FORMS = {
    "comprimido": "comprimido", "comprimidos": "comprimido", "comp": "comprimido",
    "tableta": "comprimido", "tabletas": "comprimido", "tab": "comprimido", "tabs": "comprimido",
    "capsula": "capsula", "capsulas": "capsula", "caps": "capsula",
    "jarabe": "jarabe", "suspension": "suspension", "gotas": "gotas", "crema": "crema",
    "unguento": "unguento", "gel": "gel", "ampolla": "ampolla", "ampollas": "ampolla",
    "inyectable": "inyectable", "supositorio": "supositorio", "supositorios": "supositorio",
    "solucion": "solucion", "spray": "spray", "parche": "parche", "parches": "parche",
}  # fmt: skip
SOLID_FORMS = {"comprimido", "capsula", "supositorio", "parche"}
NONSOLID_FORMS = {"jarabe", "suspension", "gotas", "crema", "unguento", "gel", "ampolla",
                  "inyectable", "solucion", "spray"}  # fmt: skip
RELEASE = {"retard": "prolongada", "lp": "prolongada", "xr": "prolongada", "sr": "prolongada",
           "cr": "prolongada", "prolongada": "prolongada"}  # fmt: skip
PACKAGING = {"caja": "caja", "frasco": "frasco", "blister": "blister", "tubo": "tubo",
             "sobre": "sobre", "envase": "envase"}  # fmt: skip
QUANTITY_UNITS = {
    "caja": "box",
    "envase": "package",
    "frasco": "bottle",
    "unidad": "unit",
    "tubo": "tube",
    "paquete": "package",
}

_FORM_MODS = {"recubiert": "recubierto", "masticabl": "masticable", "dispersabl": "dispersable",
              "efervescent": "efervescente", "sublingual": "sublingual", "ranurad": "ranurado",
              "blanda": "blanda", "dura": "dura"}  # fmt: skip
PHRASE_ALT = (
    r"(?:comprimidos?|tabletas?)\s+(?:recubiert\w*|masticabl\w*|dispersabl\w*|efervescent\w*|sublingual\w*|ranurad\w*)"
    r"|capsulas?\s+(?:blandas?|duras?)"
)
PHRASE_RE = re.compile(rf"(?<![a-z])(?:{PHRASE_ALT})(?![a-z])")
FORM_ALT = PHRASE_ALT + "|" + "|".join(sorted(FORMS, key=len, reverse=True))
# «Crema de manos» es el nombre del producto, no la forma farmacéutica «crema». Sin esta regla el
# parser dejaba el ingrediente en «manos».
_HAND_CREAM_RE = re.compile(r"(?<![a-z])crema\s+(?:de|para)\s+manos(?![a-z])")


def canonical_form(text: str) -> str | None:
    """'comprimidos recubiertos' → 'comprimido recubierto'; 'comp' → 'comprimido'; desconocida → None."""
    words = text.strip().split()
    if len(words) == 1:
        return FORMS.get(words[0])
    base = (
        FORMS.get(words[0].rstrip("s") + "s")
        or FORMS.get(words[0])
        or FORMS.get(words[0].rstrip("s"))
    )
    mod = next((v for k, v in _FORM_MODS.items() if words[1].startswith(k)), None)
    return f"{base} {mod}" if base and mod else None


_PACK_Q = r"cajas?|envases?|frascos?|unidades?|tubos?|paquetes?"
_PACK_ALL = r"caja|frasco|blister|tubo|sobre|envase"
_TAIL = rf"\s*(?:de|x)\s*(?P<cn>{NUM})(?:\s*(?P<cu>{UNIT_ALT}|{FORM_ALT})(?![a-z0-9]))?"
QTY_RE = re.compile(rf"(?<![a-z0-9.,])(?P<n>{NUM})\s*(?P<w>{_PACK_Q})(?![a-z])")
TAIL_RE = re.compile(_TAIL)
PACK_RE = re.compile(rf"(?<![a-z])(?P<w>{_PACK_ALL})s?(?![a-z])(?:{_TAIL})?")
CX_RE = re.compile(
    rf"(?<![a-z0-9])x\s*(?P<cn>{NUM})(?:\s*(?P<cu>{UNIT_ALT}|{FORM_ALT})(?![a-z0-9]))?(?![a-z0-9])"
)
CF_RE = re.compile(rf"(?<![a-z0-9.,])(?P<cn>{NUM})\s*(?P<cu>{FORM_ALT})(?![a-z])")
MEASURE_RE = re.compile(
    rf"(?<![a-z0-9.,/])(?P<n1>{NUM})\s*(?P<u1>{UNIT_ALT})(?:\s*/\s*(?P<n2>{NUM})?\s*(?P<u2>{UNIT_ALT}))?(?![a-z0-9])"
)


def normalize_name(text: str) -> str:
    """Nombre comercial comparable: 'Migranol®' → 'migranol' (sin ®, tildes ni mayúsculas)."""
    t = unicodedata.normalize("NFKD", text.lower().replace("®", " ").replace("™", " "))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


# Una `variant` es aroma, sabor, color o edición. Estas raíces delatan una indicación
# terapéutica, un síntoma o un público objetivo: no son una variante comercial y no deben
# contaminar el nombre visible ni la búsqueda. La lista es deliberadamente conservadora: solo
# rechaza términos inequívocos, nunca nombres comerciales completos.
_INDICATION_STEMS = (
    "analgesic", "antiinflamat", "antigripal", "antialerg", "antidiarre", "antiacid",
    "descongestion", "antitus", "laxante", "alivio", "dolor", "fiebre", "tos", "gripe",
    "resfrio", "congestion", "acidez", "femenin", "masculin", "infantil", "pediatric", "adulto",
    "nino", "bebe",
)  # fmt: skip


def is_commercial_variant(value: str) -> bool:
    """¿El texto parece una variante comercial y no una indicación terapéutica?"""
    words = normalize_name(value).split()
    # Las raíces cortas («tos») solo rechazan la palabra exacta; si no, «tostado» dejaría de ser
    # una variante legítima. Las raíces largas sí admiten terminaciones («analgésico/a/s»).
    return not any(
        word == stem if len(stem) <= 3 else word.startswith(stem)
        for word in words
        for stem in _INDICATION_STEMS
    )


def _display_heading(text: str) -> str:
    """«Crema de manos», «Vitamina D»: minúsculas salvo las siglas escritas en mayúsculas."""
    words = text.title().split()
    return " ".join([words[0], *(w if w.isupper() else w.lower() for w in words[1:])]) if words else ""


def valid_gtin(code: str) -> bool:
    """GTIN-8/12/13/14 con dígito verificador correcto (módulo 10)."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    body, check = code[:-1], int(code[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


_ENTITY_MARKERS = {"laboratorio": "laboratory", "laboratorios": "laboratory", "lab": "laboratory",
                   "marca": "brand", "fabricante": "manufacturer"}  # fmt: skip
ENTITY_RE = re.compile(
    r"(?<![a-z])(?P<m>laboratorios?|lab|marca|fabricante)(?![a-z])\s*(?P<n>[a-z]+)"
)
GTIN_RE = re.compile(
    r"(?<![0-9.,])(?:(?:gtin|ean|codigo\s+de\s+barras?|cod)\s*)?(?P<g>\d{8}|\d{12,14})(?![0-9.,])"
)
REG_RE = re.compile(r"(?<![a-z0-9])(?P<l>[a-z])[\s-]?(?P<n>\d{3,6})/(?P<y>\d{2})(?![a-z0-9/])")


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Texto normalizado para buscar + posición original de cada carácter (para citar lo escrito).

    µ/μ → u ANTES de quitar tildes: «µg» ≠ «g».
    """
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in ch.lower():
            c = "u" if c in "µμ" else c
            d = "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x))
            for x in re.sub(r"[^a-z0-9.,%/+ ]", " ", d):
                out.append(x)
                idx.append(i)
    return convert_number_words("".join(out), idx)


# ---------------------------------------------------------------- atributos


@dataclass(frozen=True)
class Attr:
    value: str
    basis: Basis
    origin: str = "text"  # text | image | confirmation: DE DÓNDE salió el dato


@dataclass(frozen=True)
class StrengthAttr:
    measure: Measure
    kind: str  # amount_per_unit (400 mg) | concentration (100 mg/5 mL, 2,5 %)
    basis: Basis
    role_rule: str = "unit"  # por qué se le asignó este rol
    origin: str = "text"


@dataclass(frozen=True)
class ContentAttr:
    measure: Measure  # unit 'un' = conteo de unidades de la forma
    basis: Basis
    origin: str = "text"


@dataclass(frozen=True)
class ParsedProduct:
    ingredient: str
    original_text: str = ""
    strengths: tuple[StrengthAttr, ...] = ()
    form: Attr | None = None
    content: ContentAttr | None = None
    packaging: Attr | None = None
    release: Attr | None = None
    # True = `ingredient` guarda el NOMBRE COMERCIAL leído (p. ej. de una caja) porque el principio
    # activo no es visible. Es solo un ancla para agrupar: no se afirma ningún principio activo.
    name_anchor: bool = False
    # Identidad COMERCIAL (HLD §7.1): entidades distintas; ninguna se deduce de otra ni del aspecto
    # del envase. Desconocido = None. Mismo principio+potencia+presentación con otro laboratorio
    # NO es el mismo artículo comercial.
    # Nombre comercial principal leído (p. ej. «Predual»). Es DISTINTO de la marca, del laboratorio
    # y del principio activo; cuando existe, manda en el nombre visible y en la búsqueda.
    product_name: Attr | None = None
    brand: Attr | None = None  # nombre o marca comercial
    variant: Attr | None = None  # variante comercial visible (p. ej. «Berry Warm»)
    laboratory: Attr | None = None  # laboratorio farmacéutico
    manufacturer: Attr | None = None  # fabricante
    registration_holder: Attr | None = None  # titular del registro sanitario
    marketer: Attr | None = None  # comercializador / distribuidor
    gtin: Attr | None = None  # código de barras (checksum válido)
    sanitary_registration: Attr | None = None  # registro sanitario (p. ej. F-12345/20)
    unassigned: tuple[Measure, ...] = ()  # sin unidad o sin rol claro: pendientes de validación
    requested_quantity: Decimal | None = None  # sugerencia; no se aplica
    requested_unit: str | None = None

    # ---- derivados
    @property
    def pending_validation(self) -> list[dict]:
        p: list[dict] = []
        if self.name_anchor:
            p.append({"attribute": "active_ingredient", "reason": "not_stated"})
        if not self.strengths and not self.unassigned:
            p.append({"attribute": "strength", "reason": "not_stated"})
        if self.form is None:
            p.append({"attribute": "form", "reason": "not_stated"})
        if self.content is None and not self.unassigned:
            p.append({"attribute": "content", "reason": "not_stated"})
        for m in self.unassigned:
            reason = "unit_not_stated" if m.unit is None else "role_not_stated"
            p.append({"attribute": "measure", "reason": reason, "value": m.label})
        for name, a in (("strength", self.strengths), ("form", self.form), ("content", self.content),
                        ("packaging", self.packaging), ("release", self.release)):  # fmt: skip
            items = a if isinstance(a, tuple) else ([a] if a else [])
            if any(i.basis is Basis.INFERRED for i in items):
                p.append({"attribute": name, "reason": "inferred_needs_confirmation"})
        return p

    @property
    def identity_complete(self) -> bool:
        return not self.pending_validation

    # ---- identidad farmacéutica vs comercial (HLD §7.1, §9). Dos productos pueden compartir la
    # primera sin ser el mismo artículo para compras, precios y stock.
    @property
    def pharma_pending(self) -> list[dict]:
        return [x for x in self.pending_validation if x["attribute"] in PHARMA_ATTRS]

    @property
    def commercial_pending(self) -> list[dict]:
        pend = [x for x in self.pending_validation if x["attribute"] not in PHARMA_ATTRS]
        # Marca, SKU y código de producto aún no existen (catálogo comercial: fase H3): se declaran
        # desconocidos en vez de ocultarse, para que nada dependa de una identidad simplificada.
        return pend

    @property
    def pharma_identity_key(self) -> str | None:
        """principio activo + potencia/concentración + forma + liberación (solo atributos sólidos)."""
        if self.pharma_pending or self.name_anchor or not self.strengths or not self.form:
            return None
        s = "+".join(
            sorted(x.measure.normalized().key for x in self.strengths if x.measure.normalized())
        )
        return (
            f"{self.ingredient}|{s}|{self.form.value}|{self.release.value if self.release else '-'}"
        )

    def identity_summary(self) -> dict:
        return {
            "pharmaceutical": {
                "complete": self.pharma_identity_key is not None,
                "key": self.pharma_identity_key,
                "pending": self.pharma_pending,
            },
            "commercial": {
                "complete": self.pharma_identity_key is not None and not self.commercial_pending,
                "pending": self.commercial_pending,
                # Cada entidad por separado, con su procedencia. None = desconocido (jamás inventado).
                "entities": {k: ({"value": getattr(self, k).value, "basis": getattr(self, k).basis.value,
                                  "origin": getattr(self, k).origin} if getattr(self, k) else None)
                             for k in COMMERCIAL_FIELDS},
                "unknown": [k for k in COMMERCIAL_FIELDS if not getattr(self, k)],
                "product_code": None,  # SKU interno del proveedor: catálogo comercial (H3)
            },
        }  # fmt: skip

    @property
    def resolved_identity_key(self) -> str | None:
        """Solo con identidad completa y confirmada/explícita (HLD §6.4)."""
        if not self.identity_complete or not self.strengths or not self.content or not self.form:
            return None
        s = "+".join(
            sorted(x.measure.normalized().key for x in self.strengths if x.measure.normalized())
        )
        c = self.content.measure.normalized()
        assert c
        base = f"{self.ingredient}|{s}|{self.form.value}|{c.key}|{self.release.value if self.release else '-'}"
        # Un laboratorio/marca/GTIN conocido separa artículos comerciales distintos (nunca un mismo SKU).
        extra = "".join(f"|{k}:{getattr(self, k).value.lower()}" for k in COMMERCIAL_FIELDS
                        if getattr(self, k) and getattr(self, k).basis in SOLID)  # fmt: skip
        return base + extra

    @property
    def commercial_name(self) -> Attr | None:
        """Nombre comercial que debe mandar en pantalla y búsqueda.

        `product_name` (lo que dice el envase) tiene prioridad sobre `brand` (marca genérica). Si
        `name_anchor` es True, `ingredient` ya guarda el nombre leído y no se duplica aquí: así se
        conserva el tratamiento histórico de productos sin principio activo visible.
        """
        if self.name_anchor:
            return None
        return self.product_name or self.brand

    @property
    def search_query(self) -> str:
        """Consulta corta y útil para las cadenas: nombre comercial si existe; si no, lo disponible.

        Nunca concatena una descripción farmacológica larga cuando hay un nombre comercial: eso es
        justamente lo que hacía fallar la búsqueda de «Predual».
        """
        commercial = self.commercial_name
        if commercial:
            return " ".join(commercial.value.split())
        parts = [_display_heading(self.ingredient)]
        parts.extend(s.measure.label for s in self.strengths)
        if not self.strengths and self.content and self.content.measure.unit in ("ml", "l", "g"):
            parts.append(self.content.measure.label)
        return " ".join(p for p in parts if p).strip()

    @property
    def search_query_full(self) -> str:
        """Consulta ampliada con lo que el reporte sí registró.

        Suma al nombre la concentración, la forma farmacéutica, la liberación y el contenido
        (p. ej. «30»), y descarta tramos repetidos completos: nunca deja «325» sin su «mg»
        recortando palabras sueltas. Nunca completa atributos que nadie informó ni usa
        anotaciones de revisión: si falta un dato, simplemente no aparece. `search_query` sigue
        siendo la consulta corta de respaldo.
        """
        name = (
            self.commercial_name.value
            if self.commercial_name is not None
            else _display_heading(self.ingredient)
        )
        parts: list[str] = [name, *(s.measure.label for s in self.strengths)]
        if self.form is not None:
            parts.append(self.form.value)
        if self.release is not None:
            parts.append(self.release.value)
        if self.content is not None:
            measure = self.content.measure
            parts.append(fmt(measure.value) + ("" if measure.unit == "un" else f" {measure.unit}"))
        vistos: set[str] = set()
        elegidos: list[str] = []
        for part in parts:
            tramo = " ".join(part.split())
            clave = tramo.casefold()
            if not clave or clave in vistos:
                continue
            vistos.add(clave)
            elegidos.append(tramo)
        return " ".join(elegidos)

    def display_parts(self) -> tuple[str, list[str]]:
        """Nombre para mostrar, separado en encabezado y detalles (dosis, forma, contenido).

        `" ".join([name, *details])` es EXACTAMENTE `display_name`: la identidad y la información
        no cambian. La separación existe solo para que la interfaz pueda presentar nombres largos
        en dos niveles sin recortarlos.
        """
        commercial = self.commercial_name
        name = _display_heading(commercial.value if commercial else self.ingredient)
        # Una indicación («ANALGÉSICO FEMENINO») nunca es una variante comercial. Los registros
        # históricos pueden traerla guardada; se ignora al mostrar sin reescribir la base.
        variant = self.variant.value if self.variant and is_commercial_variant(self.variant.value) else None
        details: list[str] = []
        if commercial:
            # Con nombre comercial, la variante real es un detalle, no parte del nombre.
            if variant:
                details.append(f"variante {variant}")
        elif variant:
            # Sin nombre comercial se conserva el formato histórico («Crema de manos, variante …»).
            name += f", variante {variant}"
        details += [s.measure.label for s in self.strengths]
        details += [
            f"{fmt(m.value)} (sin unidad)" if m.unit is None else f"{m.label} (rol por confirmar)"
            for m in self.unassigned
        ]
        if self.form:
            details.append(self.form.value)
        if self.release:
            details.append(self.release.value)
        if self.content:
            c = self.content.measure
            details.append(f"x {fmt(c.value)}" + ("" if c.unit == "un" else f" {c.unit}"))
        return name, details

    @property
    def display_name(self) -> str:
        name, details = self.display_parts()
        return " ".join([name, *details])

    # ---- (de)serialización, esquema v2
    def to_dict(self) -> dict:
        d: dict = {
            "schema_version": SCHEMA_VERSION, "ingredient": self.ingredient,
            "original_text": self.original_text,
            "strengths": [{"measure": s.measure.to_dict(), "kind": s.kind, "basis": s.basis.value,
                           "role_rule": s.role_rule, "origin": s.origin} for s in self.strengths],
            "unassigned": [m.to_dict() for m in self.unassigned],
            "name_anchor": self.name_anchor,
        }  # fmt: skip
        for k in ("form", "packaging", "release", "variant", "product_name", *COMMERCIAL_FIELDS):
            a = getattr(self, k)
            if a:
                d[k] = {"value": a.value, "basis": a.basis.value, "origin": a.origin}
        if self.content:
            d["content"] = {
                "measure": self.content.measure.to_dict(),
                "basis": self.content.basis.value,
                "origin": self.content.origin,
            }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ParsedProduct":
        def attr(k):
            return (
                Attr(d[k]["value"], Basis(d[k]["basis"]), d[k].get("origin", "text"))
                if d.get(k)
                else None
            )

        c = d.get("content")
        return cls(
            ingredient=d["ingredient"], original_text=d.get("original_text", ""),
            strengths=tuple(StrengthAttr(Measure.from_dict(s["measure"]), s["kind"], Basis(s["basis"]),
                                         s.get("role_rule", "unit"), s.get("origin", "text")) for s in d.get("strengths", [])),
            form=attr("form"), packaging=attr("packaging"), release=attr("release"),
            variant=attr("variant"), product_name=attr("product_name"),
            **{k: attr(k) for k in COMMERCIAL_FIELDS},
            content=ContentAttr(Measure.from_dict(c["measure"]), Basis(c["basis"]), c.get("origin", "text")) if c else None,
            unassigned=tuple(Measure.from_dict(m) for m in d.get("unassigned", [])),
            name_anchor=bool(d.get("name_anchor", False)),
        )  # fmt: skip


# ---------------------------------------------------------------- interpretación


def _kind(m: Measure) -> str:
    return "concentration" if m.is_compound or m.unit == "%" else "amount_per_unit"


def parse_shortage_text(text: str, *, allow_no_product: bool = False) -> ParsedProduct:
    """`allow_no_product`: pie de foto con solo cantidad («faltan 2 cajas») sin nombre de producto."""
    original = text.strip()
    raw, pos = _norm_map(text)

    def written(start: int, end: int) -> str:
        """Lo que la persona escribió en ese tramo (con su mayúscula, tilde y símbolo)."""
        return " ".join(text[pos[start] : pos[end - 1] + 1].split()) if end > start else ""

    if re.search(r"\bllego\b|\bllegaron\b", raw):
        raise DomainError(
            "NOT_A_SHORTAGE",
            "Eso parece una llegada, no un faltante. Identifica el pedido o la recepción.",
        )
    req_q: Decimal | None = None
    req_u: str | None = None
    content: ContentAttr | None = None
    packaging: Attr | None = None
    forms: set[str] = set()

    def cut(m: re.Match, end: int | None = None) -> None:
        """Marca el tramo como consumido SIN cambiar posiciones (así se puede citar lo escrito)."""
        nonlocal raw
        e = end if end is not None else m.end()
        raw = raw[: m.start()] + " " * (e - m.start()) + raw[e:]

    def content_from(cn: str, cu: str | None, basis: Basis, orig: str = "") -> ContentAttr | None:
        """Contenido del envase. Una unidad de potencia (mg, UI, %…) NO puede ser contenido."""
        orig = orig or f"{cn} {cu}".strip()
        if cu is None:
            return ContentAttr(Measure(parse_number(cn), "un", original_text=orig), basis)
        f = canonical_form(cu)
        if f:
            forms.add(f)
            return ContentAttr(Measure(parse_number(cn), "un", original_text=orig), basis)
        u = canonical_unit(cu)
        if u in ("ml", "l", "g", "un", "dosis"):
            return ContentAttr(Measure(parse_number(cn), u, original_text=orig), basis)
        return None

    # 1) Cantidad solicitada con unidad comercial ("5 cajas"); "5 cajas de 20" aporta además el envase.
    m = QTY_RE.search(raw)
    if m:
        word = m.group("w").rstrip("s").replace("unidade", "unidad")
        word = "unidad" if word.startswith("unidad") else word
        req_q, req_u = parse_number(m.group("n")), QUANTITY_UNITS.get(word)
        end = m.end()
        if word in PACKAGING:
            t = TAIL_RE.match(raw, m.end())
            c = (
                content_from(
                    t.group("cn"), t.group("cu"), Basis.EXPLICIT, written(t.start(), t.end())
                )
                if t
                else None
            )
            if t and c:
                content, packaging, end = c, Attr(PACKAGING[word], Basis.EXPLICIT), t.end()
        cut(m, end)

    # 2) Envase explícito: "caja de 20 comprimidos", "frasco de 120 ml", "tubo".
    for m in list(PACK_RE.finditer(raw)):
        c = (
            content_from(m.group("cn"), m.group("cu"), Basis.EXPLICIT, written(m.start(), m.end()))
            if m.group("cn")
            else None
        )
        if m.group("cn") and not c:
            continue
        packaging = Attr(PACKAGING[m.group("w")], Basis.EXPLICIT)
        content = c or content
        cut(m)
        break

    # 3) Contenido con marcador "x 20" / "x 120 ml" (explícito) o "20 comprimidos" (puede ser un
    #    conteo solicitado: queda INFERIDO hasta confirmar).
    m = CX_RE.search(raw)
    if m and content is None:
        c = content_from(m.group("cn"), m.group("cu"), Basis.EXPLICIT, written(m.start(), m.end()))
        if c:
            content = c
            cut(m)
    m = CF_RE.search(raw)
    if m and content is None:
        content = content_from(
            m.group("cn"), m.group("cu"), Basis.INFERRED, written(m.start(), m.end())
        )
        cut(m, m.start("cu"))  # conserva la palabra de la forma para detectarla abajo

    for m in list(PHRASE_RE.finditer(raw)):  # «comprimidos recubiertos» suelto
        f = canonical_form(m.group(0))
        if f:
            forms.add(f)
            cut(m)

    # 3b) Identidad comercial escrita: «lab saval», «marca advil», código de barras, registro sanitario.
    commercial: dict[str, Attr] = {}
    for m in list(ENTITY_RE.finditer(raw)):
        word = m.group("n")
        if word in STOPWORDS or word in FORMS or word.isdigit() or canonical_unit(word):
            continue
        commercial.setdefault(
            _ENTITY_MARKERS[m.group("m")], Attr(written(m.start("n"), m.end("n")), Basis.EXPLICIT)
        )
        cut(m)
    m = REG_RE.search(raw)
    if m:
        commercial["sanitary_registration"] = Attr(
            f"{m.group('l').upper()}-{m.group('n')}/{m.group('y')}", Basis.EXPLICIT
        )
        cut(m)
    for m in list(GTIN_RE.finditer(raw)):
        if valid_gtin(m.group("g")):
            commercial.setdefault("gtin", Attr(m.group("g"), Basis.EXPLICIT))
            cut(m)

    # 4) Medidas con unidad: se separan y se asigna un rol; nada se asume.
    measures: list[Measure] = []
    for m in list(MEASURE_RE.finditer(raw)):
        u1, u2 = (
            canonical_unit(m.group("u1")),
            canonical_unit(m.group("u2")) if m.group("u2") else None,
        )
        meas = Measure(parse_number(m.group("n1")), u1, parse_number(m.group("n2")) if m.group("n2") else None,
                       u2, written(m.start(), m.end()))  # fmt: skip
        try:
            meas.validate()
        except ValueError:
            continue  # unidad compuesta no soportada: queda en el texto y se trata como pendiente
        measures.append(meas)
    raw = MEASURE_RE.sub(lambda mm: " " * len(mm.group(0)) if _valid(mm) else mm.group(0), raw)

    spans = [(m.group().strip(".,/+"), m.start(), m.end()) for m in re.finditer(r"\S+", raw)]
    spans = [(t, a, b) for t, a, b in spans if t and t not in STOPWORDS]

    # 4b) Cantidad solicitada al comienzo del mensaje, sin unidad comercial («10 acidos
    # mefenamicos», «3 ibuprofeno 600mg»). Solo cuando no hubo una cantidad con unidad («2
    # cajas») y el número no se consumió antes como medida farmacéutica («10 mg amoxicilina»:
    # MEASURE_RE ya lo separó). El mensaje debe continuar con el nombre del producto, nunca con
    # una unidad, una forma, un envase ni otro número: «2 en 1» no es una cantidad de 2. El
    # número se consume, así que no queda además como medida sin unidad en la identidad.
    if req_q is None and len(spans) >= 2:
        token, start, end = spans[0]
        siguiente = spans[1][0].lower()
        if (
            re.fullmatch(r"\d{1,6}", token)
            and parse_number(token) > 0
            and any(ch.isalpha() for ch in siguiente)
            and siguiente not in FORMS
            and siguiente not in PACKAGING
            and siguiente not in RELEASE
            and canonical_unit(siguiente) is None
        ):
            req_q, req_u = parse_number(token), None
            spans = spans[1:]

    tokens = [t for t, _, _ in spans]
    forms |= {FORMS[t] for t in tokens if t in FORMS}
    hand_cream = _HAND_CREAM_RE.search(_norm_map(original)[0])
    if hand_cream and forms == {"crema"}:
        forms = set()  # «crema de manos» es nombre comercial, no forma farmacéutica
    if len(forms) > 1 and len({f.split()[0] for f in forms}) == 1:
        forms = {
            max(forms, key=len)
        }  # «comprimidos» + «comprimidos recubiertos»: la más específica
    if len(forms) > 1:
        raise DomainError("AMBIGUOUS_PRODUCT", "El texto menciona más de una forma farmacéutica.")
    form = Attr(next(iter(forms)), Basis.EXPLICIT) if forms else None
    release_t = next((RELEASE[t] for t in tokens if t in RELEASE), None)
    tokens = [t for t in tokens if t not in FORMS and t not in RELEASE and t not in PACKAGING]
    # 5) Roles por dimensión. Contexto de forma solo para 'g' (1 g comprimido vs 30 g crema).
    strengths: list[StrengthAttr] = []
    unassigned: list[Measure] = []
    for meas in measures:
        dim = UNITS[meas.unit or "un"].dimension
        if meas.is_compound or dim in (Dimension.PERCENT, Dimension.IU, Dimension.SUBSTANCE, Dimension.EQUIVALENT) \
                or meas.unit in ("mg", "mcg"):  # fmt: skip
            strengths.append(StrengthAttr(meas, _kind(meas), Basis.EXPLICIT))
        elif dim in (Dimension.VOLUME, Dimension.DOSE, Dimension.COUNT):
            if content is None:
                content = ContentAttr(meas, Basis.EXPLICIT)
            else:
                unassigned.append(meas)  # dos candidatos a contenido: no se elige
        elif meas.unit == "g":
            fv = form.value if form else None
            if fv and fv.split()[0] in SOLID_FORMS:
                strengths.append(
                    StrengthAttr(meas, "amount_per_unit", Basis.INFERRED, "form_context")
                )
            elif fv and fv.split()[0] in NONSOLID_FORMS and content is None:
                content = ContentAttr(meas, Basis.INFERRED)
            else:
                unassigned.append(meas)

    # 6) Números sueltos de 2+ cifras: valor conocido, unidad y rol DESCONOCIDOS (nunca mg).
    ing_tokens: list[str] = []
    for t, a, b in spans:
        if t in FORMS or t in RELEASE or t in PACKAGING:
            continue
        if re.fullmatch(NUM, t) and len(re.sub(r"\D", "", t)) >= 2:
            unassigned.append(Measure(parse_number(t), None, original_text=written(a, b)))
        else:
            ing_tokens.append(t)
    ingredient = " ".join(ing_tokens).strip()
    if hand_cream and form is None:
        rest = re.sub(r"(?<![a-z])manos(?![a-z])", "", ingredient, count=1).strip()
        ingredient = hand_cream.group(0) + (f" {rest}" if rest else "")
    if not ingredient and not allow_no_product:
        raise DomainError("PRODUCT_NOT_RECOGNIZED", "No pude identificar qué producto falta.")
    return ParsedProduct(
        ingredient=ingredient, original_text=original, strengths=tuple(strengths), form=form,
        content=content, packaging=packaging, release=Attr(release_t, Basis.EXPLICIT) if release_t else None,
        **commercial,
        unassigned=tuple(unassigned), requested_quantity=req_q, requested_unit=req_u,
    )  # fmt: skip


def _valid(mm: re.Match) -> bool:
    u1 = canonical_unit(mm.group("u1"))
    u2 = canonical_unit(mm.group("u2")) if mm.group("u2") else None
    try:
        Measure(Decimal(1), u1, None, u2).validate()
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- compatibilidad


class Relation(StrEnum):
    IDENTICAL = "identical"
    PARTIAL = "compatible_partial"  # hay datos faltantes/no confirmados: decide una persona
    INCOMPATIBLE = "incompatible"


@dataclass
class Comparison:
    relation: Relation
    reasons: list[str] = field(default_factory=list)


def _agree(*attrs) -> bool:
    """¿Coinciden de forma que permita tratarlos como lo mismo? Sólidos (explícitos/confirmados), o
    TODOS deducidos de la misma fuente (dos lecturas de imagen coinciden: mismo dato aún sin confirmar).
    Una inferencia frente a un dato explícito NO coincide: ahí decide una persona."""
    return _solid(*attrs) or (
        all(x.basis is Basis.INFERRED for x in attrs) and len({x.origin for x in attrs}) == 1
    )


def _solid(*attrs) -> bool:
    return all(a.basis in SOLID for a in attrs)


def compare(a: ParsedProduct, b: ParsedProduct) -> Comparison:
    """Relación entre dos descripciones. Una diferencia dura gana sobre todo; la información
    faltante o no confirmada nunca produce IDENTICAL (HLD inv. 5-6)."""
    if a.ingredient != b.ingredient:
        return Comparison(Relation.INCOMPATIBLE, ["ingredient_differs"])
    hard: list[str] = []
    soft: list[str] = []

    def keys(strengths):
        return sorted(s.measure.normalized().key for s in strengths if s.measure.normalized())

    # potencia / concentración
    if a.strengths and b.strengths:
        if keys(a.strengths) == keys(b.strengths):
            if not _agree(*a.strengths, *b.strengths):
                soft.append("strength_unconfirmed")
        elif _solid(*a.strengths, *b.strengths):
            hard.append("strength_differs")
        else:
            soft.append("strength_conflicts_with_inferred")
    elif a.strengths or b.strengths:
        soft.append("strength_unknown_one_side")
    # contenido
    if a.content and b.content:
        if a.content.measure.normalized() == b.content.measure.normalized():
            if not _agree(a.content, b.content):
                soft.append("content_unconfirmed")
        elif _solid(a.content, b.content):
            hard.append("content_differs")
        else:
            soft.append("content_conflicts_with_inferred")
    elif a.content or b.content:
        soft.append("content_unknown_one_side")
    # forma y liberación
    for name in ("form", "release"):
        x, y = getattr(a, name), getattr(b, name)
        if x and y:
            if x.value != y.value:
                (hard if _solid(x, y) else soft).append(f"{name}_differs")
            elif not _agree(x, y):
                soft.append(f"{name}_unconfirmed")
        elif x or y:
            soft.append(f"{name}_unknown_one_side")
    for k in COMMERCIAL_FIELDS:  # misma identidad farmacéutica, ¿otro artículo comercial?
        x, y = getattr(a, k), getattr(b, k)
        if x and y and x.value.lower() != y.value.lower():
            soft.append(f"{k}_differs")
    if a.packaging and b.packaging and a.packaging.value != b.packaging.value:
        soft.append("packaging_differs")  # informativo: nunca fusiona solo, tampoco separa

    # medidas sin unidad/rol: se comparan por cifra; la unidad desconocida jamás se completa
    def nums(p: ParsedProduct) -> set[Decimal]:
        out = set()
        for m in [s.measure for s in p.strengths] + ([p.content.measure] if p.content else []):
            out |= {m.value}
            n = m.normalized()
            if n:
                out.add(n.value)
        return out

    def has_info(p: ParsedProduct) -> bool:
        return bool(p.strengths or p.content or p.unassigned)

    for x, y in ((a, b), (b, a)):
        for u in x.unassigned:
            twins = [v for v in y.unassigned if v.value == u.value and v.unit == u.unit]
            if twins:
                continue
            nu = (
                u.normalized()
            )  # una medida con unidad (p. ej. 1 g) también calza por su valor normalizado
            mine = {u.value} | ({nu.value} if nu else set())
            if mine & nums(y) or any(v.value in mine for v in y.unassigned):
                soft.append("unit_unconfirmed")
            elif has_info(y):
                hard.append("no_numeric_match")
            else:
                soft.append("unit_unknown_one_side")
    rel = Relation.INCOMPATIBLE if hard else Relation.PARTIAL if soft else Relation.IDENTICAL
    return Comparison(rel, sorted(set(hard or soft)))


def classify(a: ParsedProduct, b: ParsedProduct) -> Relation:
    return compare(a, b).relation


# ---------------------------------------------------------------- confirmación humana


@dataclass
class ConfirmPatch:
    """Lo que una persona autorizada confirma. Cada campo entregado pasa a CONFIRMED."""

    ingredient: str | None = (
        None  # principio activo confirmado (reemplaza un ancla por nombre comercial)
    )
    strengths: list[Measure] | None = None  # REEMPLAZA las potencias (todas pasan a confirmadas)
    add_strengths: list[Measure] | None = None  # AÑADE; no toca el resto ni su procedencia
    form: str | None = None
    content: Measure | None = None
    release: str | None = None
    packaging: str | None = None
    confirm_inferred: set[str] = field(
        default_factory=set
    )  # strength|form|content|packaging|release


def apply_confirmation(p: ParsedProduct, patch: ConfirmPatch) -> ParsedProduct:
    C = Basis.CONFIRMED
    out = p
    if patch.ingredient:
        out = replace(out, ingredient=patch.ingredient.strip().lower(), name_anchor=False)
    if patch.strengths is not None:
        vals = {m.value for m in patch.strengths}
        out = replace(
            out,
            strengths=tuple(
                StrengthAttr(m, _kind(m), C, "confirmed", "confirmation") for m in patch.strengths
            ),
            unassigned=tuple(u for u in out.unassigned if u.value not in vals),
        )
    if patch.add_strengths:
        vals = {m.value for m in patch.add_strengths}
        out = replace(
            out,
            strengths=out.strengths
            + tuple(
                StrengthAttr(m, _kind(m), C, "confirmed", "confirmation")
                for m in patch.add_strengths
            ),
            unassigned=tuple(u for u in out.unassigned if u.value not in vals),
        )
    if patch.content is not None:
        out = replace(
            out,
            content=ContentAttr(patch.content, C, "confirmation"),
            unassigned=tuple(u for u in out.unassigned if u.value != patch.content.value),
        )
    for name in ("form", "release", "packaging"):
        v = getattr(patch, name)
        if v:
            out = replace(out, **{name: Attr(v.strip().lower(), C, "confirmation")})
    ci = patch.confirm_inferred
    if "strength" in ci:
        out = replace(out, strengths=tuple(replace(s, basis=C) for s in out.strengths))
    if "content" in ci and out.content:
        out = replace(out, content=replace(out.content, basis=C))
    for name in ("form", "release", "packaging"):
        a = getattr(out, name)
        if name in ci and a:
            out = replace(out, **{name: replace(a, basis=C)})
    return out


def apply_alias(p: ParsedProduct, aliases: list[dict]) -> ParsedProduct:
    """Aplica memoria verificada por una persona: 'para este producto, 400 a secas = 400 mg'."""
    if not p.unassigned or not aliases:
        return p
    by_value = {a["source_value"]: a["assignment"] for a in aliases}
    strengths, content, rest = list(p.strengths), p.content, []
    for u in p.unassigned:
        a = by_value.get(fmt(u.value)) if u.unit is None else None
        if not a:
            rest.append(u)
            continue
        m = build_measure(u.value, a["unit_expr"], u.original_text)
        if a["role"] == "strength":
            strengths.append(StrengthAttr(m, _kind(m), Basis.CONFIRMED, "verified_alias"))
        elif a["role"] == "content" and content is None:
            content = ContentAttr(m, Basis.CONFIRMED)
        else:
            rest.append(u)
    return replace(p, strengths=tuple(strengths), content=content, unassigned=tuple(rest))
