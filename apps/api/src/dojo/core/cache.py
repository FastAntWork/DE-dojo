"""Клиент Redis.

Redis здесь не только кеш: в нём живут блокировки стендов лаб и очереди
задач Runner-а. Поэтому в compose у него maxmemory-policy=noeviction —
вытесненный ключ-блокировка означал бы два стенда на одной лабе.
"""

from __future__ import annotations

from typing import Final

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from dojo.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT: Final = 5.0


class CacheNotConnectedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("клиент не инициализирован: вызови connect() до обращения к Redis")


class Cache:
    """Владелец пула соединений Redis. Один экземпляр на процесс."""

    def __init__(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url = url
        self._timeout = timeout
        self._client: aioredis.Redis | None = None

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise CacheNotConnectedError
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = aioredis.from_url(
            self._url,
            socket_timeout=self._timeout,
            socket_connect_timeout=self._timeout,
            decode_responses=True,
        )
        logger.info("redis.client.opened")

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None
        logger.info("redis.client.closed")

    async def ping(self) -> bool:
        """Проверка для /readyz. Не бросает: результат — часть ответа."""
        try:
            await self.client.ping()
        except (RedisError, OSError, CacheNotConnectedError, TimeoutError):
            logger.warning("redis.ping.failed", exc_info=True)
            return False
        return True
