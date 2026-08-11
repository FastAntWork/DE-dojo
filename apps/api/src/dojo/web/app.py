"""Сборка приложения.

Фабрика, а не модульный синглтон: тесты создают приложение со своими
настройками, не трогая переменные окружения процесса.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from dojo.content.registry import find_content_root, load_index
from dojo.core.cache import Cache
from dojo.core.config import Settings, get_settings
from dojo.core.db import Database
from dojo.core.logging import configure_logging, get_logger
from dojo.web.routers import content, health, ui

logger = get_logger(__name__)

STATIC_DIR: Final = Path(__file__).parent / "static"


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

        # Контент читается с диска один раз: он меняется коммитом, а не сам
        # по себе. Каталог смонтирован томом, поэтому правка узла видна после
        # перезапуска контейнера, без пересборки образа.
        content_root = settings.content_dir or find_content_root()
        app.state.content = load_index(content_root, content_root.parent)
        # Базы учебных датасетов готовятся лениво, при первом обращении к
        # практике: большинству занятий они не нужны вовсе.
        app.state.dataset_dsns = {}

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
        # ReDoc выключен: Swagger UI хватает, две страницы документации незачем.
        redoc_url=None,
        # Штатная страница /docs подтягивает свой JS и CSS с CDN, а это ломает
        # обещание работы без интернета. Отключаем её и собираем свою из
        # ассетов, лежащих в репозитории.
        docs_url=None,
    )
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} — документация",
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
        )

    app.include_router(health.router)
    app.include_router(content.router)
    # Страницы регистрируются последними: их маршруты самые общие, и роутер
    # с префиксом /api должен получать запросы первым.
    app.include_router(ui.router)
    return app


app = create_app()
