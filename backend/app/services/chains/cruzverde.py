"""Conector E15 de Cruz Verde: API pública de productos → fichas normalizadas.

La consulta, la sesión anónima y la normalización viven en `app.services.cruzverde`; este adaptador
aplica el interruptor y la configuración propios de la cadena. No comparte estado ni código de
extracción con Ahumada ni con Salcobrand: si Cruz Verde falla, las otras cadenas no se ven
afectadas.
"""

import logging
from typing import Any

from app.core.config import get_settings
from app.services import cruzverde
from app.services.chains.base import ChainNotConfigured, ChainUnavailable

logger = logging.getLogger(__name__)

CHAIN = "cruz_verde"


async def search(query: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if CHAIN not in settings.chain_search_enabled or not settings.cruzverde_live_search_enabled:
        raise ChainNotConfigured("access_not_authorized")

    try:
        return await cruzverde.search_products(
            query,
            base_url=settings.cruzverde_live_search_base_url or cruzverde.DEFAULT_BASE_URL,
            login_url=settings.cruzverde_live_search_login_url or cruzverde.DEFAULT_LOGIN_URL,
            sitemap_url=settings.cruzverde_live_search_sitemap_url
            or cruzverde.DEFAULT_SITEMAP_URL,
            timeout_seconds=settings.cruzverde_live_search_timeout_seconds,
            budget_seconds=settings.cruzverde_live_search_budget_seconds,
        )
    except cruzverde.CruzVerdeQueryRejected as exc:
        # El buscador rechazó la consulta: no es una fuente caída ni «sin resultados».
        raise ChainUnavailable("query_rejected") from exc
    except cruzverde.CruzVerdeFetchError as exc:
        raise ChainUnavailable("fetch_failed") from exc
