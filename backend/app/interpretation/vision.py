"""Contrato de visión multimodal y traducción determinista.

El proveedor real es opcional (`VISION_PROVIDER=openai`, adaptador en `openai_vision.py`).

Separación de responsabilidades (HLD §8, §17.3):
  * Un `VisionInterpreter` (futuro proveedor de IA con visión) solo DEVUELVE una `VisionExtraction`:
    texto visible por atributo, con confianza. No decide nada y no conoce las reglas farmacéuticas.
  * Este módulo traduce esa extracción con las MISMAS reglas deterministas que un texto escrito
    (`parse_shortage_text`, unidades, GTIN, compatibilidad). Todo lo leído de una imagen queda
    `inferred` con `origin="image"`: un modelo nunca confirma un atributo.
  * Lo que no es visible permanece desconocido. El laboratorio NO se deduce del aspecto del envase,
    ni el fabricante del laboratorio, ni el titular del registro del fabricante.
  * Una cantidad visible en el envase (cajas apiladas, «4 cápsulas») NUNCA es la cantidad solicitada
    para comprar: esa solo la dice la persona en el texto.

`SimulatedVisionInterpreter` existe solo para pruebas/demo y marca `mode="simulated"` en cada
resultado: jamás debe presentarse como lectura real de una imagen.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal, Protocol

from app.core.errors import DomainError
from app.domain.products import (
    COMMERCIAL_FIELDS,
    REG_RE,
    Attr,
    Basis,
    ContentAttr,
    ParsedProduct,
    normalize_name,
    parse_shortage_text,
    valid_gtin,
)


class VisionStatus(StrEnum):
    PENDING = "pending"  # recibida; aún sin procesar (p. ej. sin proveedor de visión)
    PROCESSING = "processing"
    IDENTIFIED = (
        "identified"  # hay producto identificable; igual requiere confirmación de atributos
    )
    NEEDS_REVIEW = "needs_review"  # borrosa, incompleta, ambigua o en conflicto con el texto
    ERROR = "error"


# Atributos que un proveedor puede reportar. Cada uno es independiente de los demás.
VISION_ATTRIBUTES = (
    "ingredient", "product_name", "variant", "strength", "concentration", "content", "form",
    "packaging", "release", *COMMERCIAL_FIELDS, "visible_quantity",
)  # fmt: skip
MIN_CONFIDENCE = 0.5


@dataclass(frozen=True)
class VisionField:
    attribute: str  # uno de VISION_ATTRIBUTES
    text: str  # texto tal cual se ve en la imagen
    confidence: float  # 0..1 informado por el proveedor; solo sirve para descartar lo dudoso


@dataclass(frozen=True)
class VisionExtraction:
    mode: Literal["live", "simulated"]
    provider: str
    model: str
    legibility: Literal["clear", "partial", "blurry", "unreadable"]
    fields: tuple[VisionField, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"mode": self.mode, "provider": self.provider, "model": self.model,
                "legibility": self.legibility, "warnings": list(self.warnings),
                "fields": [{"attribute": f.attribute, "text": f.text, "confidence": f.confidence}
                           for f in self.fields]}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> "VisionExtraction":
        return cls(d["mode"], d["provider"], d["model"], d["legibility"],
                   tuple(VisionField(f["attribute"], f["text"], float(f["confidence"])) for f in d.get("fields", [])),
                   tuple(d.get("warnings", [])))  # fmt: skip


class VisionUnavailable(RuntimeError):
    """No hay proveedor de visión conectado."""


class VisionFailed(RuntimeError):
    """El proveedor de visión falló. `code` nunca se muestra al usuario.

    `http_status`, `provider_code`, `request_id` y `detail` son datos técnicos **ya saneados**
    para el diagnóstico interno (registro y auditoría): nunca contienen la clave, la imagen, el
    texto leído ni el cuerpo de la respuesta del proveedor. `detail` señala **qué rama** del
    análisis local rechazó la respuesta (p. ej. `output_not_json`), sin guardar su contenido.
    """

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        detail: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.provider_code = provider_code
        self.request_id = request_id
        self.detail = detail


class VisionInterpreter(Protocol):
    async def extract(self, image: bytes, mime: str, caption: str | None) -> VisionExtraction: ...


class NullVisionInterpreter:
    """Por defecto: no hay visión. La foto se guarda y queda `pending`; nada se inventa."""

    async def extract(self, image: bytes, mime: str, caption: str | None) -> VisionExtraction:
        raise VisionUnavailable("no hay un proveedor de visión conectado")


class SimulatedVisionInterpreter:
    """SIMULACIÓN para pruebas y demo. Devuelve resultados predefinidos, etiquetados `simulated`."""

    def __init__(
        self, scripts: dict[str, VisionExtraction], default: VisionExtraction | None = None
    ):
        self.scripts, self.default = scripts, default

    async def extract(self, image: bytes, mime: str, caption: str | None) -> VisionExtraction:
        sha = hashlib.sha256(image).hexdigest()
        ext = self.scripts.get(sha) or self.scripts.get("*") or self.default
        if ext is None:
            raise VisionUnavailable("la simulación no tiene un resultado para esta imagen")
        return replace(ext, mode="simulated", provider="simulator", model="simulated-v0")


def load_simulation(path: str) -> SimulatedVisionInterpreter:
    data = json.loads(open(path, encoding="utf-8").read())
    return SimulatedVisionInterpreter({k: VisionExtraction.from_dict(v) for k, v in data.items()})


# ------------------------------------------------------------------ traducción determinista


@dataclass
class ImageReading:
    parsed: ParsedProduct | None  # None si no hay producto identificable
    status: VisionStatus
    uncertainties: list[str] = field(default_factory=list)
    ignored: list[str] = field(
        default_factory=list
    )  # lo que se vio y NO se usó (p. ej. cantidad visible)


def _tag_image(p: ParsedProduct) -> ParsedProduct:
    """Todo lo leído de una imagen es una inferencia (jamás confirmada) con origen `image`."""
    I = Basis.INFERRED  # noqa: E741
    return replace(
        p,
        strengths=tuple(
            replace(s, basis=I, origin="image", role_rule="image") for s in p.strengths
        ),
        form=p.form and replace(p.form, basis=I, origin="image"),
        release=p.release and replace(p.release, basis=I, origin="image"),
        packaging=p.packaging and replace(p.packaging, basis=I, origin="image"),
        content=p.content and ContentAttr(p.content.measure, I, "image"),
    )


def read_extraction(ext: VisionExtraction) -> ImageReading:
    """Convierte lo que el proveedor «vio» en un producto, con las reglas del dominio."""
    if ext.legibility in ("blurry", "unreadable"):
        return ImageReading(None, VisionStatus.NEEDS_REVIEW, [f"image_{ext.legibility}"])
    unc: list[str] = []
    ignored: list[str] = []
    use: dict[str, list[str]] = {}
    for f in ext.fields:
        if f.attribute not in VISION_ATTRIBUTES:
            unc.append(f"unknown_vision_attribute:{f.attribute}")
        elif f.attribute == "visible_quantity":
            ignored.append("visible_quantity_not_a_requested_quantity")
        elif f.confidence < MIN_CONFIDENCE or not f.text.strip():
            unc.append(f"low_confidence:{f.attribute}")
        else:
            use.setdefault(f.attribute, []).append(f.text.strip())
    # Un medicamento combinado tiene varios principios activos (p. ej. tramadol + paracetamol):
    # se conservan todos, en el orden leído, y sus dosis siguen en `strengths` (una por
    # componente). El parser determinista ya acepta esa forma; no se elige uno ni se descarta
    # la lectura por tener más de uno. El mismo principio escrito dos veces se cuenta una sola.
    ingredients: list[str] = []
    seen_ingredients: set[str] = set()
    for name in use.get("ingredient") or []:
        key = normalize_name(name)
        if key and key not in seen_ingredients:
            seen_ingredients.add(key)
            ingredients.append(name)
    ingredient = " ".join(ingredients) if ingredients else None
    brand_seen = (use.get("brand") or use.get("product_name") or [None])[0]
    anchor = False
    if not ingredient:
        unc.append("active_ingredient_not_visible")
        if not brand_seen:
            return ImageReading(
                None, VisionStatus.NEEDS_REVIEW, [*unc, "no_product_name_visible"], ignored
            )
        # Solo se ve el nombre comercial: sirve de ANCLA para registrar el faltante y agrupar avisos,
        # sin afirmar ningún principio activo (no se adivina por el nombre ni por el aspecto).
        ingredient, anchor = normalize_name(brand_seen), True
    parts = [ingredient, *use.get("strength", []), *use.get("concentration", []), *use.get("form", []),
             *use.get("release", []), *use.get("packaging", [])]  # fmt: skip
    if use.get("content"):
        parts.append("x " + use["content"][0])
    try:
        p = parse_shortage_text(" ".join(parts))
    except DomainError as e:
        return ImageReading(
            None, VisionStatus.NEEDS_REVIEW, [*unc, f"unparseable:{e.code}"], ignored
        )
    p = replace(_tag_image(p), name_anchor=anchor)
    # La misma dosis escrita de dos formas («37,5 mg» y «37.5 mg») es una sola: no debe
    # duplicarse en la identidad ni en el nombre mostrado.
    seen_strengths: set[str] = set()
    strengths = []
    for s in p.strengths:
        norm = s.measure.normalized()
        key = norm.key if norm else s.measure.label
        if key not in seen_strengths:
            seen_strengths.add(key)
            strengths.append(s)
    if len(strengths) != len(p.strengths):
        p = replace(p, strengths=tuple(strengths))
    variant = (use.get("variant") or [None])[0]
    if variant:
        p = replace(p, variant=Attr(variant, Basis.INFERRED, "image"))
    if not p.strengths and not p.unassigned:
        unc.append("strength_not_visible")
    ent: dict[str, Attr] = {}
    for k in COMMERCIAL_FIELDS:
        v = (use.get(k) or [None])[0]
        if not v:
            continue
        if k == "gtin":
            digits = "".join(c for c in v if c.isdigit())
            if not valid_gtin(digits):
                unc.append("gtin_checksum_invalid")
                continue
            v = digits
        if k == "sanitary_registration":
            m = REG_RE.search(v.lower().replace("-", " "))
            v = f"{m.group('l').upper()}-{m.group('n')}/{m.group('y')}" if m else v
        ent[k] = Attr(v, Basis.INFERRED, "image")
    if use.get("product_name") and "brand" not in ent:
        ent["brand"] = Attr(
            use["product_name"][0], Basis.INFERRED, "image"
        )  # tal como se ve (con ®)
    p = replace(p, original_text="[imagen] " + " ".join(parts), **ent)
    status = VisionStatus.IDENTIFIED if ext.legibility == "clear" else VisionStatus.NEEDS_REVIEW
    if ext.legibility == "partial":
        unc.append("image_partially_legible")
    return ImageReading(p, status, unc, ignored)


# ------------------------------------------------------------------ foto + texto


def merge_text_and_image(
    text: ParsedProduct | None, image: ParsedProduct
) -> tuple[ParsedProduct, list[str]]:
    """Combina el pie de foto con lo leído de la imagen. Contradicción → conflicto (AC14): jamás se
    resuelve sola. La cantidad solicitada sale SOLO del texto."""
    if text is None:
        return image, []
    conflicts: list[str] = []
    out = image
    if text.ingredient:
        if text.ingredient != image.ingredient:
            conflicts.append("ingredient")
        out = replace(out, ingredient=text.ingredient)

    def pick(name: str, tv, iv):
        if tv and iv:
            same = tv.value == iv.value if hasattr(tv, "value") else None
            if same is False:
                conflicts.append(name)
            return tv
        return tv or iv

    def mkeys(xs):
        return sorted(x.measure.normalized().key for x in xs if x.measure.normalized())

    strengths = image.strengths
    if text.strengths and image.strengths:
        if mkeys(text.strengths) != mkeys(image.strengths):
            conflicts.append("strength")
        strengths = text.strengths
    elif text.strengths:
        strengths = text.strengths
    content = text.content or image.content
    if (
        text.content
        and image.content
        and text.content.measure.normalized() != image.content.measure.normalized()
    ):
        conflicts.append("content")
    # cifra suelta del texto que coincide con una potencia leída de la imagen: la unidad viene de la
    # imagen (inferida, nunca confirmada); la cifra deja de estar pendiente.
    vals = {s.measure.value for s in image.strengths}
    unassigned = (
        tuple(u for u in text.unassigned if u.value not in vals)
        if image.strengths
        else text.unassigned
    )
    out = replace(
        out, strengths=strengths, content=content, unassigned=unassigned, original_text=text.original_text,
        form=pick("form", text.form, image.form), release=pick("release", text.release, image.release),
        packaging=pick("packaging", text.packaging, image.packaging),
        variant=pick("variant", text.variant, image.variant),
        requested_quantity=text.requested_quantity, requested_unit=text.requested_unit,
        **{k: pick(k, getattr(text, k), getattr(image, k)) for k in COMMERCIAL_FIELDS},
    )  # fmt: skip
    return out, conflicts
