"""Пул соединений с Postgres.

Обёртка тонкая сознательно: она управляет жизненным циклом пула и умеет
себя проверять, но не прячет SQL. Запросы пишутся руками — см. docs/adr/0003.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from dojo.core.logging import get_logger

logger = get_logger(__name__)

# Postgres в compose поднят с max_connections=50. Пул держим заметно меньше:
# при 16 ГБ хоста упереться в память рабочих процессов проще, чем в лимит.
DEFAULT_MIN_SIZE: Final = 2
DEFAULT_MAX_SIZE: Final = 10
# Запрос, висящий дольше минуты, — почти наверняка ошибка, а не медленный отчёт.
DEFAULT_COMMAND_TIMEOUT: Final = 60.0


class DatabaseNotConnectedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("пул не инициализирован: вызови connect() до обращения к БД")


class Database:
    """Владелец пула. Один экземпляр на процесс, живёт в lifespan приложения."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    @property
    def pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self._pool is None:
            raise DatabaseNotConnectedError
        return self._pool

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=self._command_timeout,
        )
        logger.info("postgres.pool.opened", min_size=self._min_size, max_size=self._max_size)

    async def close(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        logger.info("postgres.pool.closed")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PoolConnectionProxy[asyncpg.Record]]:
        # Пул отдаёт не Connection, а прокси поверх него: прокси возвращает
        # соединение в пул при выходе из контекста и умеет пережить его
        # переоткрытие. Интерфейс тот же, тип — другой.
        async with self.pool.acquire() as conn:
            yield conn

    async def ping(self) -> bool:
        """Проверка для /readyz. Не бросает: результат — часть ответа, а не ошибка."""
        try:
            async with self.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except (asyncpg.PostgresError, OSError, DatabaseNotConnectedError, TimeoutError):
            logger.warning("postgres.ping.failed", exc_info=True)
            return False
        return True
