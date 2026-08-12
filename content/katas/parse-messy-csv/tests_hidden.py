"""Скрытые тесты: краевые случаи. Пользователю не показываются."""

from __future__ import annotations

from decimal import Decimal

from solution import parse_orders


def test_column_order_from_header() -> None:
    """Порядок колонок в источнике меняется — разбор обязан это переживать."""
    lines = ["city,order_id,amount", "Москва,7,55.55"]

    result = parse_orders(lines)

    assert result.rows == [{"order_id": 7, "amount": Decimal("55.55"), "city": "Москва"}]


def test_empty_input() -> None:
    result = parse_orders([])

    assert result.rows == []
    assert result.rejected == []


def test_header_only() -> None:
    result = parse_orders(["order_id,amount,city"])

    assert result.rows == []
    assert result.rejected == []


def test_missing_required_column_is_reported() -> None:
    result = parse_orders(["order_id,city", "1,Москва"])

    assert result.rows == []
    assert len(result.rejected) == 1
    assert "amount" in result.rejected[0].reason


def test_line_numbers_count_header_as_first() -> None:
    """Номер строки должен совпадать с тем, что покажет редактор."""
    lines = [
        "order_id,amount,city",
        "1,10.00,Москва",
        "2,abc,Казань",
    ]

    result = parse_orders(lines)

    assert result.rejected[0].line_no == 3


def test_empty_amount_is_rejected() -> None:
    result = parse_orders(["order_id,amount,city", "1,,Москва"])

    assert result.rows == []
    assert len(result.rejected) == 1


def test_empty_order_id_is_rejected() -> None:
    result = parse_orders(["order_id,amount,city", ",10.00,Москва"])

    assert result.rows == []
    assert len(result.rejected) == 1


def test_various_missing_markers_for_city() -> None:
    lines = [
        "order_id,amount,city",
        "1,10.00,",
        "2,10.00,NA",
        "3,10.00,-",
        "4,10.00,не указано",
    ]

    result = parse_orders(lines)

    assert result.rejected == []
    assert [row["city"] for row in result.rows] == [None, None, None, None]


def test_whitespace_is_stripped() -> None:
    result = parse_orders(["order_id,amount,city", "  1 ,  10.00  ,  Москва  "])

    assert result.rows == [{"order_id": 1, "amount": Decimal("10.00"), "city": "Москва"}]


def test_decimal_precision_is_kept() -> None:
    """Три раза по 0.10 должны дать ровно 0.30 — иначе взяли float."""
    lines = ["order_id,amount,city"] + [f"{i},0.10,Москва" for i in range(1, 4)]

    result = parse_orders(lines)

    assert sum(row["amount"] for row in result.rows) == Decimal("0.30")


def test_reason_mentions_the_bad_value() -> None:
    """Причина брака должна быть пригодна для разбора, а не «ошибка»."""
    result = parse_orders(["order_id,amount,city", "1,abc,Москва"])

    assert "abc" in result.rejected[0].reason


def test_all_rows_bad() -> None:
    lines = ["order_id,amount,city", "x,y,z", "q,w,e"]

    result = parse_orders(lines)

    assert result.rows == []
    assert len(result.rejected) == 2


def test_quoted_value_with_newline_inside() -> None:
    """Перенос строки внутри кавычек — часть значения, а не новая запись."""
    lines = ["order_id,amount,city", '1,10.00,"Москва\nЗеленоград"']

    result = parse_orders(lines)

    assert len(result.rows) + len(result.rejected) == 1


def test_negative_and_large_amounts_pass_through() -> None:
    """Бизнес-правила — не дело разбора: он проверяет формат, а не смысл."""
    lines = ["order_id,amount,city", "1,-5.00,Москва", "2,999999999.99,Казань"]

    result = parse_orders(lines)

    assert len(result.rows) == 2
