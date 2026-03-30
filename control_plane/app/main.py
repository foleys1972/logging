"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from .config import get_settings
from .api.v1.endpoints import router as api_router
from .database import get_engine
from .models import Base


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.on_event("startup")
    async def _startup() -> None:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    return app


app = create_app()


