"""Выгрузка всех страниц из API с ретраями.

Заготовка: Response и ApiError менять не нужно, замени тело fetch_all.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class ApiError(RuntimeError):
    """Выгрузка не удалась и повторять её бессмысленно."""


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
    """Забирает все страницы, повторяя временные сбои.

    Повторяются 429, 5xx и TimeoutError. На прочих 4xx бросается ApiError.
    Задержка растёт экспоненциально; retry_after от сервера важнее её.
    После max_attempts неудач подряд — ApiError.
    """
    raise NotImplementedError


__all__ = ["RETRYABLE_STATUSES", "ApiError", "Response", "Transport", "fetch_all"]
