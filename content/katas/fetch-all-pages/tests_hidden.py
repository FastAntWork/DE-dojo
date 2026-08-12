"""Скрытые тесты: то, что легко упустить. Пользователю не показываются."""

from __future__ import annotations

from typing import Any

import pytest
from solution import ApiError, Response, fetch_all


class FakeTransport:
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
    return Response(status=200, items=[{"id": i} for i in items], next_cursor=next_cursor)


def test_timeout_is_retried() -> None:
    transport = FakeTransport([TimeoutError("сеть"), page([1])])

    assert fetch_all(transport, sleep=lambda _: None) == [{"id": 1}]


def test_attempt_counter_resets_after_success() -> None:
    """Сбой на десятой странице не должен учитывать неудачи на второй."""
    transport = FakeTransport(
        [
            Response(status=500),
            Response(status=500),
            page([1], "c1"),
            Response(status=500),
            Response(status=500),
            page([2]),
        ]
    )

    assert fetch_all(transport, max_attempts=3, sleep=lambda _: None) == [{"id": 1}, {"id": 2}]


def test_delay_grows_exponentially() -> None:
    delays: list[float] = []
    transport = FakeTransport([Response(status=500)] * 3 + [page([1])])

    fetch_all(transport, base_delay=1.0, max_attempts=5, sleep=delays.append)

    assert delays == [1.0, 2.0, 4.0], f"задержки не растут: {delays}"


def test_retry_after_wins_over_own_delay() -> None:
    delays: list[float] = []
    transport = FakeTransport([Response(status=429, retry_after=30.0), page([1])])

    fetch_all(transport, base_delay=1.0, sleep=delays.append)

    assert delays == [30.0], "просьба сервера важнее собственной задержки"


def test_no_sleep_on_success() -> None:
    delays: list[float] = []
    transport = FakeTransport([page([1], "c1"), page([2])])

    fetch_all(transport, sleep=delays.append)

    assert delays == [], "между успешными страницами спать незачем"


def test_repeating_cursor_stops() -> None:
    """Сервер, отдающий прежний курсор, зациклил бы выгрузку."""
    transport = FakeTransport([page([1], "same"), page([2], "same"), page([3], "same")] * 10)

    with pytest.raises(ApiError):
        fetch_all(transport, sleep=lambda _: None)


def test_empty_first_page_is_not_an_error() -> None:
    transport = FakeTransport([Response(status=200, items=[], next_cursor=None)])

    assert fetch_all(transport, sleep=lambda _: None) == []


def test_empty_page_in_the_middle_is_followed() -> None:
    transport = FakeTransport(
        [page([1], "c1"), Response(status=200, items=[], next_cursor="c2"), page([2])]
    )

    assert fetch_all(transport, sleep=lambda _: None) == [{"id": 1}, {"id": 2}]


def test_limit_is_passed_through() -> None:
    transport = FakeTransport([page([1])])

    fetch_all(transport, limit=7, sleep=lambda _: None)

    assert transport.calls[0][1] == 7


def test_first_request_has_no_cursor() -> None:
    transport = FakeTransport([page([1])])

    fetch_all(transport, sleep=lambda _: None)

    assert transport.calls[0][0] is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    transport = FakeTransport([Response(status=status)] * 5)

    with pytest.raises(ApiError):
        fetch_all(transport, sleep=lambda _: None)

    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(status: int) -> None:
    transport = FakeTransport([Response(status=status), page([1])])

    assert fetch_all(transport, sleep=lambda _: None) == [{"id": 1}]


def test_error_message_is_useful() -> None:
    """По сообщению должно быть понятно, что случилось, без чтения кода."""
    transport = FakeTransport([Response(status=503)] * 5)

    with pytest.raises(ApiError) as info:
        fetch_all(transport, max_attempts=3, sleep=lambda _: None)

    assert "503" in str(info.value)


def test_records_from_all_pages_are_kept_in_order() -> None:
    transport = FakeTransport([page([1, 2], "c1"), page([3, 4], "c2"), page([5])])

    records = fetch_all(transport, sleep=lambda _: None)

    assert [record["id"] for record in records] == [1, 2, 3, 4, 5]
