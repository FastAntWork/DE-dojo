"""Сборка приложения.

Фабрика, а не модульный синглтон: тесты создают приложение со своими
настройками, не трогая переменные окружения процесса.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dojo.core.cache import Cache
from dojo.core.config import Settings, get_settings
from dojo.core.db import Database
from dojo.core.logging import configure_logging, get_logger
from dojo.web.routers import health

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Локально — цветной консольный вывод, в остальных случаях JSON.
    configure_logging(settings.log_level, json_output=not settings.is_local)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        cache = Cache(settings.redis_url)
        app.state.settings = settings
        app.state.db = db
        app.state.cache = cache

        # Падать на старте из-за недоступной БД нельзя: `docker compose up`
        # стал бы гонкой за порядок готовности контейнеров. Незаполненный
        # пул честно отражается красным /readyz.
        for name, resource in (("postgres", db), ("redis", cache)):
            try:
                await resource.connect()
            except OSError:
                logger.warning("startup.connect.failed", resource=name)

        logger.info("app.started", env=settings.env)
        try:
            yield
        finally:
            await cache.close()
            await db.close()
            logger.info("app.stopped")

    app = FastAPI(
        title="DE Dojo API",
        version="0.0.0",
        lifespan=lifespan,
        # ReDoc выключен: Swagger UI хватает, две страницы документации
        # незачем. Учти, что и Swagger UI грузит свой JS с CDN, поэтому
        # обещание «работает офлайн» он пока не выполняет — ассеты надо
        # положить локально до приёмки M0 по критерию офлайна (см. README).
        redoc_url=None,
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app


app = create_app()
