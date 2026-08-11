"""Скрытые тесты. В задании их нет — иначе решение подгоняют под них.

Здесь проверяется то, что легко упустить: краевые случаи, ленивость и момент
возникновения ошибки.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from solution import chunked


def test_empty_input_yields_nothing() -> None:
    assert list(chunked([], 3)) == []


def test_error_raised_at_call_not_at_iteration() -> None:
    """ValueError обязан возникать в момент вызова.

    Если функция написана генератором целиком, её тело не выполнится до
    первого next(), и проверка аргумента отложится. Тест ловит именно это:
    исключение должно возникнуть до того, как кто-либо начнёт итерацию.
    """
    with pytest.raises(ValueError):
        chunked([1, 2, 3], -1)

    with pytest.raises(ValueError):
        chunked([1, 2, 3], 0)


def test_accepts_generator_input() -> None:
    source = (i for i in range(5))

    assert list(chunked(source, 2)) == [[0, 1], [2, 3], [4]]


def test_is_lazy() -> None:
    """Из источника читается только необходимое.

    Реализация, начинающаяся с list(items), провалит этот тест: она прочитает
    все сто элементов, хотя запрошены две пачки по три.
    """
    consumed = 0

    def counting() -> Iterator[int]:
        nonlocal consumed
        for i in range(100):
            consumed += 1
            yield i

    first_two = list(itertools.islice(chunked(counting(), 3), 2))

    assert first_two == [[0, 1, 2], [3, 4, 5]]
    assert consumed <= 7, f"прочитано {consumed} элементов вместо шести-семи"


def test_works_with_infinite_source() -> None:
    """Бесконечный источник не должен приводить к зависанию."""
    result = list(itertools.islice(chunked(itertools.count(), 4), 3))

    assert result == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]


def test_returns_lists_not_tuples() -> None:
    chunk = next(iter(chunked([1, 2, 3], 2)))

    assert isinstance(chunk, list)


def test_does_not_reuse_chunk_object() -> None:
    """Каждая пачка — отдельный список.

    Переиспользование одного списка с очисткой через clear() даёт верный
    результат при немедленном потреблении и мусор при отложенном.
    """
    chunks = list(chunked(range(6), 2))

    assert chunks == [[0, 1], [2, 3], [4, 5]]


@given(
    data=st.lists(st.integers(), max_size=50),
    size=st.integers(min_value=1, max_value=10),
)
def test_concatenation_restores_input(data: list[int], size: int) -> None:
    """Свойство: склейка всех пачек обязана дать исходные данные.

    Property-based тест ловит краевые случаи, которые автор не догадался
    перечислить руками.
    """
    chunks = list(chunked(data, size))

    assert [item for chunk in chunks for item in chunk] == data
    assert all(len(chunk) == size for chunk in chunks[:-1])
    if chunks:
        assert 0 < len(chunks[-1]) <= size
