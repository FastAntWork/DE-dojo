"""Открытые тесты. Их видно в задании — это примеры того, что ожидается."""

from __future__ import annotations

from typing import Any

import pytest
from solution import ApiError, Response, fetch_all


class FakeTransport:
    """Отдаёт заранее заданную последовательность ответов."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str | None, int]] = []

    def get(self, cursor: str | None, limit: int) -> Response:
        self.calls.append((cursor, limit))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def page(items: list[int], next_cursor: str | None = None) -> Response:
    return Response(
        status=200,
        items=[{"id": i} for i in items],
        next_cursor=next_cursor,
    )


def test_single_page() -> None:
    transport = FakeTransport([page([1, 2, 3])])

    assert fetch_all(transport, sleep=lambda _: None) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_follows_cursor_through_pages() -> None:
    transport = FakeTransport([page([1], "c1"), page([2], "c2"), page([3])])

    records = fetch_all(transport, sleep=lambda _: None)

    assert records == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [cursor for cursor, _ in transport.calls] == [None, "c1", "c2"]


def test_retries_server_error() -> None:
    transport = FakeTransport([Response(status=503), page([1])])

    assert fetch_all(transport, sleep=lambda _: None) == [{"id": 1}]


def test_does_not_retry_client_error() -> None:
    transport = FakeTransport([Response(status=401), page([1])])

    with pytest.raises(ApiError):
        fetch_all(transport, sleep=lambda _: None)

    assert len(transport.calls) == 1, "401 повторять бессмысленно"


def test_gives_up_after_max_attempts() -> None:
    transport = FakeTransport([Response(status=500)] * 10)

    with pytest.raises(ApiError):
        fetch_all(transport, max_attempts=3, sleep=lambda _: None)

    assert len(transport.calls) == 3
