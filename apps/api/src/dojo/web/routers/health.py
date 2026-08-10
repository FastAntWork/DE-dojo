"""Пробы живости и готовности.

Разделение принципиальное, а не косметическое:

* `/healthz` — процесс жив. Никаких зависимостей. Если она красная, приложение
  надо перезапускать.
* `/readyz` — приложение способно обслуживать запросы: доступны Postgres и
  Redis. Красная readiness при зелёной liveness означает «подожди, но не
  перезапускай» — перезапуск от недоступной БД ничего не лечит, а лишь
  добавляет шума.

Смешать их в одну ручку — классический способ устроить рестарт-шторм в тот
момент, когда БД и так под нагрузкой.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from dojo.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@runtime_checkable
class Probeable(Protocol):
    """Ресурс, который умеет подключаться и отвечать на пинг."""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def ping(self) -> bool: ...


class HealthResponse(BaseModel):
    status: str
    env: str


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]


async def probe(resource: Probeable) -> bool:
    """Пингует ресурс, при необходимости подняв соединение.

    Приложение стартует, даже если БД была недоступна в момент запуска:
    иначе `docker compose up` превращался бы в гонку за порядок готовности
    контейнеров. Первая же удачная проба поднимает пул.
    """
    if not resource.is_connected:
        try:
            await resource.connect()
        except OSError:
            logger.warning("readyz.connect.failed", resource=type(resource).__name__)
            return False
    return await resource.ping()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", env=request.app.state.settings.env)


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    checks = {
        "postgres": await probe(request.app.state.db),
        "redis": await probe(request.app.state.cache),
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readyz.not_ready", checks=checks)
    return ReadyResponse(ready=ready, checks=checks)
