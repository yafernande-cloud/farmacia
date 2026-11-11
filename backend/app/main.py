import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import (
    catalog,
    comparisons,
    connectors,
    demand,
    health,
    import_profiles,
    imports,
    observations,
    photos,
    quotes,
    search,
    sources,
    staff,
    supplier_mappings,
    suppliers,
    telegram,
    telegram_accounts,
    web_access,
)
from app.core.config import get_settings
from app.core.errors import DomainError, correlation_id_var
from app.db.session import DatabaseNotConfigured, dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    app = FastAPI(
        title="Faltantes Farmacia — Asistente de Compras",
        version="0.1.0",
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        correlation_id_var.set(cid)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(exc.to_body(), status_code=exc.status_code)

    @app.exception_handler(DatabaseNotConfigured)
    async def db_missing(_: Request, __: DatabaseNotConfigured):
        err = DomainError(
            "DATABASE_NOT_CONFIGURED", "La base de datos no está configurada.", status_code=503
        )
        return JSONResponse(err.to_body(), status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        err = DomainError(
            "VALIDATION_ERROR",
            "La solicitud no es válida.",
            status_code=400,
            details={"fields": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
        )
        return JSONResponse(err.to_body(), status_code=400)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(demand.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(suppliers.router, prefix="/api/v1")
    app.include_router(sources.router, prefix="/api/v1")
    app.include_router(imports.router, prefix="/api/v1")
    app.include_router(import_profiles.router, prefix="/api/v1")
    app.include_router(connectors.router, prefix="/api/v1")
    app.include_router(supplier_mappings.router, prefix="/api/v1")
    app.include_router(staff.router, prefix="/api/v1")
    app.include_router(telegram_accounts.router, prefix="/api/v1")
    app.include_router(telegram.router, prefix="/api/v1")
    app.include_router(web_access.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(comparisons.router, prefix="/api/v1")
    app.include_router(quotes.router, prefix="/api/v1")
    app.include_router(observations.router, prefix="/api/v1")
    app.include_router(photos.router, prefix="/api/v1")
    return app


app = create_app()
