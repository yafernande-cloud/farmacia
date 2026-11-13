"""Conector E15 de Farmacias Ahumada: HTML público → fichas normalizadas.

La lógica de extracción vive en `app.services.ahumada`; este adaptador solo aplica el interruptor
y la configuración propios de la cadena y traduce cualquier fallo a los estados del coordinador.
"""

from typing import Any

from app.core.config import get_settings
from app.services import ahumada
from app.services.chains.base import ChainNotConfigured, ChainUnavailable

CHAIN = "ahumada"


async def search(query: str) -> list[dict[str, Any]]:
    settings = get_settings()
    # Sin declaración explícita de fuente autorizada no se hace ninguna solicitud (D-115).
    if CHAIN not in settings.chain_search_enabled or not settings.ahumada_live_search_enabled:
        raise ChainNotConfigured("access_not_authorized")
    if not settings.ahumada_live_search_url:
        raise ChainNotConfigured("live_source_not_configured")

    try:
        html_text = await ahumada.fetch_search_html(
            query,
            url=settings.ahumada_live_search_url,
            timeout_seconds=settings.ahumada_live_search_timeout_seconds,
        )
        return ahumada.parse_search_results(html_text, query=query)
    except ahumada.AhumadaFetchError as exc:
        raise ChainUnavailable("fetch_failed") from exc
