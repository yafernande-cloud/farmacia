"""Conector E15 de Salcobrand: índice público de Algolia → fichas normalizadas.

La consulta y la normalización viven en `app.services.salcobrand`; este adaptador aplica el
interruptor y la configuración propios de la cadena. No comparte estado ni código de extracción
con las otras cadenas: si Salcobrand falla, Ahumada no se ve afectada.
"""

from typing import Any
from urllib.parse import urljoin

from app.core.config import get_settings
from app.services import salcobrand
from app.services.chains.base import ChainNotConfigured, ChainUnavailable

CHAIN = "salcobrand"


async def search(query: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if CHAIN not in settings.chain_search_enabled or not settings.salcobrand_live_search_enabled:
        raise ChainNotConfigured("access_not_authorized")

    url = settings.salcobrand_live_search_url
    app_id = settings.salcobrand_live_search_app_id
    api_key = settings.salcobrand_live_search_api_key
    referer = settings.salcobrand_live_search_referer
    if not url or not app_id or not api_key or not referer:
        raise ChainNotConfigured("live_source_not_configured")

    try:
        # La hora del filtro sale de la configuración del sitio, igual que en su buscador. El
        # reloj reutiliza el desfase medido y no consulta la configuración en cada búsqueda.
        moment = await salcobrand.clock.now(
            url=urljoin(referer, salcobrand.PREFERENCES_PATH),
            referer=referer,
            timeout_seconds=settings.salcobrand_live_search_timeout_seconds,
        )
        payload = await salcobrand.fetch_search_json(
            query,
            url=url,
            app_id=app_id,
            api_key=api_key.get_secret_value(),
            index=settings.salcobrand_live_search_index,
            referer=referer,
            timeout_seconds=settings.salcobrand_live_search_timeout_seconds,
            moment=moment,
        )
        return salcobrand.parse_search_results(payload)
    except salcobrand.SalcobrandFetchError as exc:
        raise ChainUnavailable("fetch_failed") from exc
