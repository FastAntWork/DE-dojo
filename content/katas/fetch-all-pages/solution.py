"""Эталонное решение. Пользователю не показывается."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    items: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    retry_after: float | None = None


class Transport(Protocol):
    def get(self, cursor: str | None, limit: int) -> Response: ...


RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def fetch_all(
    transport: Transport,
    limit: int = 100,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    attempt = 0

    while True:
        try:
            response = transport.get(cursor=cursor, limit=limit)
        except TimeoutError:
            response = None

        if response is not None and response.status == 200:
            records.extend(response.items)
            attempt = 0  # счётчик сбрасывается после каждого успеха

            if response.next_cursor is None:
                return records

            # Сервер, отдающий прежний курсор, зациклил бы выгрузку.
            if response.next_cursor in seen_cursors:
                msg = f"сервер вернул повторяющийся курсор {response.next_cursor!r}"
                raise ApiError(msg)
            seen_cursors.add(response.next_cursor)
            cursor = response.next_cursor
            continue

        # Ошибка клиента повторов не переживёт: чинить надо запрос, а не ждать.
        if response is not None and response.status not in RETRYABLE_STATUSES:
            msg = f"неповторяемая ошибка: статус {response.status}"
            raise ApiError(msg)

        attempt += 1
        if attempt >= max_attempts:
            what = "таймаут" if response is None else f"статус {response.status}"
            msg = f"не удалось получить страницу за {max_attempts} попыток ({what})"
            raise ApiError(msg)

        # Просьба сервера важнее собственной оценки.
        delay = base_delay * 2 ** (attempt - 1)
        if response is not None and response.retry_after is not None:
            delay = response.retry_after
        sleep(delay)


__all__ = ["RETRYABLE_STATUSES", "ApiError", "Response", "Transport", "fetch_all"]
