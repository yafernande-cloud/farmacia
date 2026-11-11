"""Vigencia de las listas importadas (D-151).

`offer_snapshot` es de solo agregar: cada importación añade observaciones y nada se borra. Para
leer «la lista vigente» se toma, por fuente, la última corrida con filas aceptadas y se muestran
solo sus ofertas. Una corrida fallida o sin filas aceptadas no reemplaza la última lista válida, y
las filas que la última lista no trae dejan de presentarse como precio actual sin perder su
historial. Las fuentes en vivo no tienen `import_run`: conservan la lectura de siempre.
"""

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportRun, OfferSnapshot


async def latest_valid_run_ids(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    source_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, uuid.UUID]:
    """Última corrida con filas aceptadas por fuente (empate: la más reciente y de id mayor)."""
    rank = (
        func.row_number()
        .over(
            partition_by=ImportRun.source_id,
            order_by=(ImportRun.imported_at.desc(), ImportRun.id.desc()),
        )
        .label("rank")
    )
    query = select(ImportRun.id.label("run_id"), ImportRun.source_id, rank).where(
        ImportRun.organization_id == org,
        ImportRun.rows_accepted > 0,
    )
    if source_ids is not None:
        query = query.where(ImportRun.source_id.in_(list(source_ids)))
    ranked = query.subquery()
    rows = await session.execute(
        select(ranked.c.source_id, ranked.c.run_id).where(ranked.c.rank == 1)
    )
    return {source_id: run_id for source_id, run_id in rows.all()}


def current_list_filter(run_ids: dict[uuid.UUID, uuid.UUID]) -> Any | None:
    """Condición SQL que deja solo ofertas de la corrida vigente de cada fuente.

    Las fuentes sin corrida válida (API en vivo y fuentes históricas sin `import_run`) quedan
    fuera del filtro y mantienen su lectura anterior. Una oferta de una corrida anterior no se
    considera aunque sea la más reciente de su SKU.
    """
    if not run_ids:
        return None
    return or_(
        OfferSnapshot.source_id.not_in(list(run_ids)),
        *(
            and_(
                OfferSnapshot.source_id == source_id,
                OfferSnapshot.evidence["import_run_id"].astext == str(run_id),
            )
            for source_id, run_id in run_ids.items()
        ),
    )
