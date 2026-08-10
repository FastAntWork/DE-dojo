"""Общие фикстуры интеграционных тестов.

Контейнеры поднимаются один раз на модуль: старт Postgres занимает секунды, и
платить их на каждый тест незачем. Изоляция обеспечивается не пересозданием
контейнера, а сбросом схемы перед каждым тестом — так вдесятеро быстрее и при
этом ни один тест не зависит от того, что оставил предыдущий.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import asyncpg
import pytest

# В testcontainers 4.15 модули переехали в testcontainers.community.*;
# старые пути живы, но кричат DeprecationWarning, а он у нас = ошибка.
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

# Тот же образ, что и в compose: расширение pgvector нужно миграции 001,
# и на голом postgres:16 она не применится.
POSTGRES_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(autouse=True)
def clean_database(postgres_dsn: str) -> None:
    """Возвращает БД в состояние «только что создана».

    Фикстура синхронная и поднимает свой цикл событий: так она одинаково
    работает и для async-тестов, и для синхронных, которые ходят в приложение
    через TestClient.
    """

    async def reset() -> None:
        conn = await asyncpg.connect(postgres_dsn)
        try:
            # Расширения тоже живут в public и пересоздадутся миграцией 001.
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        finally:
            await conn.close()

    asyncio.run(reset())
