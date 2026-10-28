"""Procedencia de los datos y tipos de organización.

PROCEDENCIA ≠ CONFIANZA. `data_origin` dice DE DÓNDE viene un registro:
  simulated   simulador, fixture o prueba automatizada
  captured    recibido de una persona o dispositivo real (texto escrito, foto enviada)
  ai_derived  resultado derivado por un modelo de IA a partir de lo capturado (p. ej. lectura de imagen)
  unclassified solo en organizaciones `legacy_mixed`, hasta que se clasifique

El grado de certeza de un atributo es otra cosa (`basis`: explicit/inferred/confirmed). Un atributo
`ai_derived` puede quedar `confirmed` por una persona sin dejar de ser `ai_derived`, y un dato
`captured` puede ser solo `inferred`.
"""

import re
import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models import Organization


class DataOrigin(StrEnum):
    SIMULATED = "simulated"
    CAPTURED = "captured"
    AI_DERIVED = "ai_derived"
    UNCLASSIFIED = "unclassified"


class OrgKind(StrEnum):
    SIMULATION = "simulation"
    REAL_TEST = "real_test"
    PRODUCTION = "production"
    LEGACY_MIXED = "legacy_mixed"


# Chats ficticios reservados a la simulación (el trigger de la base aplica la misma regla).
SIM_CHAT_RE = re.compile(r"^555000[0-9]+$")


def is_simulation_chat(chat_id: str | int) -> bool:
    return bool(SIM_CHAT_RE.fullmatch(str(chat_id)))


async def org_kind(session: AsyncSession, org_id: uuid.UUID) -> str:
    return (
        await session.execute(select(Organization.kind).where(Organization.id == org_id))
    ).scalar_one()


def origin_for(kind: str, *, ai_derived: bool = False) -> str:
    """Procedencia de un registro NUEVO según la organización. Una simulación siempre es `simulated`."""
    if kind == OrgKind.SIMULATION:
        return DataOrigin.SIMULATED
    return DataOrigin.AI_DERIVED if ai_derived else DataOrigin.CAPTURED


def validate_binding(chat_id: str, kind: str) -> None:
    """Un chat ficticio solo va a simulación; una organización de simulación solo acepta chats ficticios."""
    if kind == OrgKind.LEGACY_MIXED:
        return
    sim = is_simulation_chat(chat_id)
    if sim and kind != OrgKind.SIMULATION:
        raise DomainError(
            "SIMULATION_CHAT_RESERVED",
            f"El chat {chat_id} es ficticio: solo puede vincularse a la organización de simulación.",
            status_code=422,
        )
    if not sim and kind == OrgKind.SIMULATION:
        raise DomainError(
            "REAL_CHAT_IN_SIMULATION",
            f"El chat {chat_id} no es ficticio: no puede vincularse a la organización de simulación.",
            status_code=422,
        )


def allow_simulated_vision(kind: str) -> bool:
    """Una lectura SIMULADA de imagen solo puede escribirse en la organización de simulación."""
    return kind == OrgKind.SIMULATION


def vision_guard(provider: str, kinds: list[str]) -> None:
    """VISION_PROVIDER=simulated solo es válido si TODOS los chats activos son de organizaciones de simulación."""
    if provider == "simulated" and any(k != OrgKind.SIMULATION for k in kinds):
        raise DomainError(
            "SIMULATED_VISION_WITH_REAL_CHAT",
            "VISION_PROVIDER=simulated no se puede usar con chats de organizaciones no simuladas.",
            status_code=422,
        )
