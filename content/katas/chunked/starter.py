"""Разбиение последовательности на пачки.

Заготовка: замени тело функции своим решением.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")


def chunked(items: Iterable[T], size: int) -> Iterator[list[T]]:
    """Разбивает items на списки по size элементов.

    Последняя пачка может быть неполной.
    При size <= 0 бросает ValueError сразу при вызове.
    """
    raise NotImplementedError
