"""Открытые тесты. Их видно в задании — это примеры того, что ожидается."""

from __future__ import annotations

from decimal import Decimal

from solution import parse_orders


def test_parses_simple_file() -> None:
    lines = [
        "order_id,amount,city",
        "1,120.50,Москва",
        "2,99.00,Казань",
    ]

    result = parse_orders(lines)

    assert result.rejected == []
    assert result.rows == [
        {"order_id": 1, "amount": Decimal("120.50"), "city": "Москва"},
        {"order_id": 2, "amount": Decimal("99.00"), "city": "Казань"},
    ]


def test_amount_is_decimal_not_float() -> None:
    result = parse_orders(["order_id,amount,city", "1,0.10,Москва"])

    assert isinstance(result.rows[0]["amount"], Decimal)


def test_comma_inside_quotes_is_not_a_separator() -> None:
    lines = ["order_id,amount,city", '1,120.50,"Ростов-на-Дону, центр"']

    result = parse_orders(lines)

    assert result.rows[0]["city"] == "Ростов-на-Дону, центр"


def test_bad_row_does_not_stop_parsing() -> None:
    lines = [
        "order_id,amount,city",
        "1,120.50,Москва",
        "2,abc,Казань",
        "3,10.00,Пермь",
    ]

    result = parse_orders(lines)

    assert [row["order_id"] for row in result.rows] == [1, 3]
    assert len(result.rejected) == 1


def test_missing_city_becomes_none() -> None:
    result = parse_orders(["order_id,amount,city", "1,120.50,NULL"])

    assert result.rows[0]["city"] is None
