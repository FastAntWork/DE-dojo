"""Открытые тесты. Их видно в задании — это примеры того, что ожидается."""

from __future__ import annotations

import pytest

from solution import chunked


def test_splits_evenly() -> None:
    assert list(chunked([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]


def test_last_chunk_may_be_short() -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_single_chunk() -> None:
    assert list(chunked([1, 2, 3], 10)) == [[1, 2, 3]]


def test_size_one() -> None:
    assert list(chunked("abc", 1)) == [["a"], ["b"], ["c"]]


def test_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        chunked([1, 2, 3], 0)
