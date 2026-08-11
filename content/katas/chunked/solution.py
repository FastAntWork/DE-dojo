"""Эталонное решение.

Ключевой приём — разделение функции надвое. Внешняя функция обычная, поэтому
её тело выполняется в момент вызова и проверка аргумента срабатывает сразу.
Внутренняя — генератор, и её ленивость сохраняется.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")


def chunked(items: Iterable[T], size: int) -> Iterator[list[T]]:
    if size <= 0:
        msg = f"size должен быть положительным, получено {size}"
        raise ValueError(msg)
    return _chunks(items, size)


def _chunks(items: Iterable[T], size: int) -> Iterator[list[T]]:
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
